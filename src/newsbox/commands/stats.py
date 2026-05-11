"""``newsbox stats`` — 库健康度聚合视图。

只读命令：直连 SQLite 跑 4 个聚合查询，输出 4 块面板的人类视图，或 ``--json``
结构化全量。

设计要点：
- 不走 SDK：4 块聚合都是只读 SQL，直接 ``get_conn`` 上跑更直观。
- 不调 ``init_db``：避免在仅查询命令里触发 schema 应用。
- 信源数（enabled / disabled）来自 ``sources.yaml`` 而非 ``raw.db``——库里只有"被
  抓过的信源痕迹"，不能反映"配置过的总数"。
- ``last_7_days`` cutoff 在 Python 计算（``_now_utc()`` 可注入），SQL 用
  绑定参数；测试 monkeypatch ``_now_utc`` 即可固定 ANCHOR。
- ``DATE(fetched_at)`` 不带 ``'localtime'`` 修饰符：保持 UTC 一致，与
  ``fetched_at`` 写入侧（``datetime.now(timezone.utc).isoformat()``）对齐。
- ``domain_tags`` 是 JSON 列，用 ``json_each`` 展开成行后再分组。
- 数字千位分隔（``f"{n:,}"``）只在人类视图，JSON 里保持纯数字。
- ASCII bar 长度按"该 7 天最大值线性映射到固定 25 字符"，全 0 时 bar 全空。
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import typer

from .. import sources as sources_module
from ..db import get_conn
from ._helpers import home_option
from ._json import emit, emit_err, json_option

# ---- 常量 -------------------------------------------------------------------

_TIME_KEEP = 19  # len("YYYY-MM-DD HH:MM:SS")
_BAR_WIDTH = 25  # last_7_days 直方图 bar 最大宽度（字符）
_LAST_N_DAYS = 7  # last_7_days panel 跨度


# ---- 工具 -------------------------------------------------------------------


def _now_utc() -> datetime:
    """当前 UTC 时间。

    单独抽出来便于测试 monkeypatch 注入 ANCHOR：
    ``monkeypatch.setattr(stats, "_now_utc", lambda: ANCHOR)``。
    """
    return datetime.now(timezone.utc)


def _format_time(raw: str | None) -> str | None:
    """把 ISO 时间戳格式化为 ``YYYY-MM-DD HH:MM:SS``；None 返回 None。"""
    if raw is None:
        return None
    s = str(raw).replace("T", " ")
    return s[:_TIME_KEEP]


def _read_source_counts(home: Path) -> tuple[int, int]:
    """从 ``sources.yaml`` 读 enabled / disabled 数。

    yaml 不存在或解析失败 → 返回 (0, 0)，避免 stats 因为 sources.yaml 缺失而失败。
    ``list_sources`` 返回的是 ``{kind: {total, enabled}}``；disabled = total - enabled。
    """
    yaml_path = home / "sources.yaml"
    try:
        result = sources_module.list_sources(yaml_path)
    except Exception:  # noqa: BLE001 — yaml 缺失/损坏均退化为 0
        return 0, 0
    enabled = sum(v.get("enabled", 0) for v in result.values())
    total = sum(v.get("total", 0) for v in result.values())
    disabled = max(0, total - enabled)
    return enabled, disabled


# ---- 聚合查询 ---------------------------------------------------------------


def _query_total(conn: sqlite3.Connection) -> dict[str, Any]:
    with closing(
        conn.execute(
            "SELECT COUNT(*) AS articles, "
            "       MIN(fetched_at) AS earliest, "
            "       MAX(fetched_at) AS latest "
            "FROM articles_raw"
        )
    ) as cur:
        row = cur.fetchone()
    return {
        "articles": int(row["articles"] or 0),
        "earliest_fetched_at": row["earliest"],  # ISO 字符串或 None
        "latest_fetched_at": row["latest"],
    }


def _query_top_sources(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """全量返回，按 count 降序，再按 source_id 字典升序破平。"""
    with closing(
        conn.execute(
            "SELECT source_id, source_type, COUNT(*) AS c "
            "FROM articles_raw "
            "GROUP BY source_id, source_type "
            "ORDER BY c DESC, source_id ASC"
        )
    ) as cur:
        rows = cur.fetchall()
    return [
        {
            "rank": idx + 1,
            "source_id": r["source_id"],
            "source_type": r["source_type"],
            "count": int(r["c"]),
        }
        for idx, r in enumerate(rows)
    ]


def _query_last_7_days(
    conn: sqlite3.Connection, *, now: datetime
) -> list[dict[str, Any]]:
    """最近 7 天每天的入库条数，缺失日补 0。

    cutoff 用 Python 的 ``now.date() - 6d``（含端 7 天），不用 SQL ``date('now',...)``，
    便于测试注入。SQL 仍走 ``DATE(fetched_at)``（UTC 与写入侧对齐）。
    """
    today = now.date()
    cutoff_date = today - timedelta(days=_LAST_N_DAYS - 1)

    # 用 ISO 字符串字典序对比即可（fetched_at 都是 ISO 8601 + UTC offset，
    # 且 DATE() 解析能识别）。
    cutoff_iso = cutoff_date.isoformat()

    with closing(
        conn.execute(
            "SELECT DATE(fetched_at) AS d, COUNT(*) AS c "
            "FROM articles_raw "
            "WHERE DATE(fetched_at) >= ? "
            "GROUP BY d "
            "ORDER BY d",
            (cutoff_iso,),
        )
    ) as cur:
        rows = cur.fetchall()

    found: dict[str, int] = {r["d"]: int(r["c"]) for r in rows}

    out: list[dict[str, Any]] = []
    for i in range(_LAST_N_DAYS):
        d = (cutoff_date + timedelta(days=i)).isoformat()
        out.append({"date": d, "count": found.get(d, 0)})
    return out


def _query_by_source_type_domain(
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """按 source_type × domain 分组；多 domain 行通过 json_each 展开计入多组。"""
    with closing(
        conn.execute(
            "SELECT source_type, json_each.value AS domain, COUNT(*) AS c "
            "FROM articles_raw, json_each(articles_raw.domain_tags) "
            "GROUP BY source_type, domain "
            "ORDER BY source_type, domain"
        )
    ) as cur:
        rows = cur.fetchall()
    return [
        {
            "source_type": r["source_type"],
            "domain": r["domain"],
            "count": int(r["c"]),
        }
        for r in rows
    ]


# ---- 渲染 ------------------------------------------------------------------


def _render_human(
    payload: dict[str, Any], *, top_n: int
) -> None:
    typer.echo("== newsbox stats ==")
    typer.echo("")

    # ---- [Total] -----------------------------------------------------------
    total = payload["total"]
    typer.echo("[Total]")
    typer.echo(f"  articles: {total['articles']:,}")
    typer.echo(
        f"  enabled sources: {total['enabled_sources']:,}  /  "
        f"disabled: {total['disabled_sources']:,}"
    )
    earliest = total["earliest_fetched_at"]
    latest = total["latest_fetched_at"]
    typer.echo(
        f"  earliest fetched_at: {_format_time(earliest) or '-'}"
    )
    typer.echo(
        f"  latest   fetched_at: {_format_time(latest) or '-'}"
    )
    typer.echo("")

    # ---- [Top sources by article count] ------------------------------------
    typer.echo("[Top sources by article count]")
    top_sources_full = payload["top_sources"]
    if not top_sources_full:
        typer.echo("  (no sources tracked)")
    else:
        shown = top_sources_full[:top_n]
        # 列宽
        w_id = max(len(item["source_id"]) for item in shown)
        w_id = max(w_id, len("source_id"))
        w_type = max(len(item["source_type"]) for item in shown)
        w_type = max(w_type, len("type"))
        w_count = max(len(f"{item['count']:,}") for item in shown)
        # rank 序号宽度按总条目数
        w_rank = len(str(len(shown)))
        for item in shown:
            line = (
                f"  {item['rank']:>{w_rank}}. "
                f"{item['source_id']:<{w_id}}  "
                f"{item['source_type']:<{w_type}}  "
                f"{item['count']:>{w_count},}"
            )
            typer.echo(line)
        if len(top_sources_full) > top_n:
            typer.echo(
                f"  (showing top {top_n} of {len(top_sources_full)} sources)"
            )
    typer.echo("")

    # ---- [Last 7 days new articles] ----------------------------------------
    typer.echo("[Last 7 days new articles]")
    last7 = payload["last_7_days"]
    max_count = max((row["count"] for row in last7), default=0)
    if max_count == 0:
        typer.echo("  (no articles in last 7 days)")
        # 空 panel 后仍要换行，便于下一 panel 隔开
        typer.echo("")
    else:
        w_count = max(len(f"{row['count']:,}") for row in last7)
        for row in last7:
            n = row["count"]
            bar_len = int(n / max_count * _BAR_WIDTH) if n > 0 else 0
            bar = "#" * bar_len + " " * (_BAR_WIDTH - bar_len)
            typer.echo(
                f"  {row['date']} |{bar}| {n:>{w_count},}"
            )
        typer.echo("")

    # ---- [By source_type × domain] -----------------------------------------
    typer.echo("[By source_type × domain]")
    by_td = payload["by_source_type_domain"]
    if not by_td:
        typer.echo("  (no data)")
        return
    w_type = max(len(r["source_type"]) for r in by_td)
    w_type = max(w_type, len("source_type"))
    w_domain = max(len(r["domain"]) for r in by_td)
    w_domain = max(w_domain, len("domain"))
    w_count = max(len(f"{r['count']:,}") for r in by_td)
    w_count = max(w_count, len("count"))
    typer.echo(
        f"  {'source_type':<{w_type}}  "
        f"{'domain':<{w_domain}}  "
        f"{'count':>{w_count}}"
    )
    for r in by_td:
        typer.echo(
            f"  {r['source_type']:<{w_type}}  "
            f"{r['domain']:<{w_domain}}  "
            f"{r['count']:>{w_count},}"
        )


# ---- 命令 -------------------------------------------------------------------


def stats_cmd(
    home: Path = home_option(),
    top: int = typer.Option(
        10,
        "--top",
        help="信源 Top-N 排行长度；默认 10；仅作用于人类视图（JSON 全量）",
    ),
    json_output: bool = json_option(),
) -> None:
    """库健康度统计：4 块面板 / ``--json`` 结构化全量。"""
    db_path = home / "raw.db"
    if not db_path.exists():
        if json_output:
            emit_err(
                f"raw.db not found: {db_path}",
                hint="run 'newsbox setup' or 'newsbox fetch' first",
            )
        else:
            typer.echo(
                f"[err] raw.db 未找到: {db_path}，请先 newsbox setup 或 fetch 一次",
                err=True,
            )
        raise typer.Exit(code=1)

    enabled_sources, disabled_sources = _read_source_counts(home)

    conn = get_conn(db_path)
    try:
        total = _query_total(conn)
        top_sources = _query_top_sources(conn)
        last_7_days = _query_last_7_days(conn, now=_now_utc())
        by_td = _query_by_source_type_domain(conn)
    finally:
        conn.close()

    payload: dict[str, Any] = {
        "total": {
            "articles": total["articles"],
            "enabled_sources": enabled_sources,
            "disabled_sources": disabled_sources,
            "earliest_fetched_at": total["earliest_fetched_at"],
            "latest_fetched_at": total["latest_fetched_at"],
        },
        "top_sources": top_sources,
        "last_7_days": last_7_days,
        "by_source_type_domain": by_td,
    }

    if json_output:
        emit(payload)
        return

    _render_human(payload, top_n=top)
