"""``news-collector read`` — 查询 raw.db 中的文章。

SDK ``read_raw`` 的薄 CLI 包装，给 agent 与人类调试者用。

设计要点：
- 只读：不调 ``init_db``；raw.db 不存在时 exit 1（区别于 0 条结果时 exit 0）。
- ``--source-id`` / ``--tier`` SDK 没原生支持，迭代 SDK 输出后做 Python filter。
- ``--limit=0`` 表示无限；SDK ``limit=None`` 才是无限，所以 CLI 层做 0 → None 转换。
  又因 source-id / tier 是后置 filter，所以 SDK 一律不传 limit、本命令自己 break。
- 默认 rich Table 输出（参考 commands/state.py 列宽算法）；``--json`` 走 NDJSON。
- ``--since`` 默认 "24h"；解析失败 exit 2 + stderr 报错。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import typer

from .. import sdk
from ..utils.time import parse_since
from ._helpers import home_option

# ---- 常量 -------------------------------------------------------------------

_TIME_KEEP = 19  # len("YYYY-MM-DD HH:MM:SS")


# ---- 工具 -------------------------------------------------------------------


def _now_utc() -> datetime:
    """当前 UTC 时间。

    单独抽出来便于测试 monkeypatch 注入 ANCHOR（KNOWLEDGE R-6 模式）：
    ``monkeypatch.setattr(read, "_now_utc", lambda: ANCHOR)``。
    """
    return datetime.now(timezone.utc)


def _format_time(dt: datetime) -> str:
    """datetime → ``YYYY-MM-DD HH:MM:SS``（19 字符截断 ISO）。

    本命令独立实现而非复用 state.py 的 ``_format_time``，避免污染共用 helper
    （state 处理 ``str | None``，read 处理 datetime；语义不一致）。
    """
    s = dt.isoformat().replace("T", " ")
    return s[:_TIME_KEEP]


def _parse_csv(value: str | None) -> list[str] | None:
    """逗号分隔字符串 → 列表；空/None → None。"""
    if value is None:
        return None
    parts = [p.strip() for p in value.split(",") if p.strip()]
    return parts or None


def _article_to_json_dict(art: sdk.ArticleRaw) -> dict[str, object]:
    """ArticleRaw → 可 json.dumps 的 dict。datetime → isoformat / None → null。"""
    return {
        "id": art.id,
        "source_type": art.source_type,
        "source_id": art.source_id,
        "source_tier": art.source_tier,
        "external_id": art.external_id,
        "url": art.url,
        "title": art.title,
        "body": art.body,
        "content_hash": art.content_hash,
        "published_at": art.published_at.isoformat() if art.published_at else None,
        "fetched_at": art.fetched_at.isoformat(),
        "domain_tags": list(art.domain_tags),
    }


# ---- 命令 -------------------------------------------------------------------


def read_cmd(
    home: Path = home_option(),
    since: str = typer.Option(
        "24h",
        "--since",
        help="时间窗口，按 fetched_at 过滤（如 24h / 7d / 2026-05-01）",
    ),
    source_types: str | None = typer.Option(
        None,
        "--source-types",
        help="逗号分隔过滤 source_type（rss,web）；不传不过滤",
    ),
    source_id: str | None = typer.Option(
        None,
        "--source-id",
        help="精确匹配 source_id；不传不过滤",
    ),
    domain: str = typer.Option(
        "ai",
        "--domain",
        help="domain_tag 过滤（默认 ai；任一 tag 命中即匹配）",
    ),
    tier: str | None = typer.Option(
        None,
        "--tier",
        help="逗号分隔过滤 source_tier（official_first_party,kol,secondary）",
    ),
    limit: int = typer.Option(
        50,
        "--limit",
        help="最多输出 N 条；0 表示无限",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="按 NDJSON 输出（每行一条 JSON），便于 agent 消费",
    ),
) -> None:
    """读取 raw.db 中的文章（按 fetched_at 升序）。"""
    db_path = home / "raw.db"
    if not db_path.exists():
        typer.echo(
            f"[err] raw.db 未找到: {db_path}，请先 news-collector setup 或 fetch 一次",
            err=True,
        )
        raise typer.Exit(code=1)

    # since 解析（默认 "24h"，失败 exit 2）
    # 注入 _now_utc() 让测试可锁 ANCHOR（R-6 模式）
    try:
        since_dt = parse_since(since, now=_now_utc())
    except ValueError as exc:
        typer.echo(f"[err] {exc}", err=True)
        raise typer.Exit(code=2) from exc

    source_types_list = _parse_csv(source_types)
    tier_list = _parse_csv(tier)

    # SDK limit 一律不传：source-id / tier 是后置 filter，提前 LIMIT 会丢匹配行；
    # 自己控制循环 + break，简单且 SDK 流式不会装满内存。
    iterator = sdk.read_raw(
        domain=domain,
        since=since_dt,
        source_types=source_types_list,
        limit=None,
        db_path=db_path,
    )

    # --limit=0 即无限；非 0 则按命令层 break。
    cap: int | None = None if limit == 0 else int(limit)

    collected: list[sdk.ArticleRaw] = []
    for art in iterator:
        if source_id is not None and art.source_id != source_id:
            continue
        if tier_list is not None and art.source_tier not in tier_list:
            continue
        collected.append(art)
        if cap is not None and len(collected) >= cap:
            break

    if as_json:
        for art in collected:
            typer.echo(json.dumps(_article_to_json_dict(art), ensure_ascii=False))
        raise typer.Exit(code=0)

    # ---- rich Table 输出 ----
    if not collected:
        typer.echo("  (no articles match)")
        raise typer.Exit(code=0)

    # 列宽自适应（参考 state.py 算法）
    w_time = max(len("fetched_at"), _TIME_KEEP)
    w_type = max(
        len("source_type"),
        max(len(a.source_type) for a in collected),
    )
    w_id = max(
        len("source_id"),
        max(len(a.source_id) for a in collected),
    )
    w_tier = max(
        len("tier"),
        max(len(a.source_tier) for a in collected),
    )

    header = (
        f"  {'fetched_at':<{w_time}}  "
        f"{'source_type':<{w_type}}  "
        f"{'source_id':<{w_id}}  "
        f"{'tier':<{w_tier}}  "
        f"title"
    )
    typer.echo(header)

    for a in collected:
        line = (
            f"  {_format_time(a.fetched_at):<{w_time}}  "
            f"{a.source_type:<{w_type}}  "
            f"{a.source_id:<{w_id}}  "
            f"{a.source_tier:<{w_tier}}  "
            f"{a.title}"
        )
        typer.echo(line)

    typer.echo(f"  ({len(collected)} articles, since={since})")
