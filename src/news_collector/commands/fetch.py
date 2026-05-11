"""``news-collector fetch`` 顶层命令。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from ..pipeline import fetch as fetch_pipeline
from ..utils.time import parse_since
from ._helpers import home_option, load_app_config


def fetch_cmd(
    home: Path = home_option(),
    source: str = typer.Option(
        "all",
        "--source",
        help="all / <source_type>(rss/web) / <source_id>",
    ),
    since: str = typer.Option(
        "24h",
        "--since",
        help="相对值 24h / 7d / 30m / 2w，或 ISO 8601；'all' 表示不过滤",
    ),
    concurrency: int = typer.Option(
        0,
        "--concurrency",
        help="rss 桶并发度覆盖（0 = 走 config.fetch.concurrency.rss，默认 8）；web 桶始终串行",
    ),
) -> None:
    """拉取信源入库。"""
    parsed_since = None if since.lower() == "all" else parse_since(since)
    cfg = load_app_config(home)
    conc = concurrency if concurrency > 0 else None

    summary = asyncio.run(
        fetch_pipeline.run_fetch(
            home,
            since=parsed_since,
            source_filter=source,
            config=cfg,
            concurrency=conc,
        )
    )

    if not summary.results:
        typer.echo("  (no sources matched)")
        raise typer.Exit(code=0)

    for r in summary.results:
        if r.skipped:
            typer.echo(f"  [skip] {r.source_type:<10} {r.source_id}")
        elif r.error:
            typer.echo(
                f"  [fail] {r.source_type:<10} {r.source_id:<30} err={r.error}"
            )
        else:
            typer.echo(
                f"  [ok]   {r.source_type:<10} {r.source_id:<30} "
                f"fetched={r.fetched:>3} inserted={r.inserted:>3} "
                f"dup_url={r.deduped_url:>2} dup_ext={r.deduped_external:>2}"
            )
    typer.echo(f"\n  TOTAL inserted = {summary.total_inserted}")
