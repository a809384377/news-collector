"""``newsbox sources test`` 子命令实装。

s4-sources-management Step 7 subagent E 产出。

设计要点
========
- 录入后试拉一次：**不入库**，只把 adapter 拉到的前 N 条 RawArticle 打到终端
- 用 ``home_option()`` 指定运行时目录；从 ``sources.yaml`` 里按 id 找信源，
  按 ``kind`` 分派 ``RSSAdapter`` / ``WebAdapter``，``since=None`` 拉全部最新
- adapter 抛异常 → 捕获 + stderr 打印异常类型 + Exit(1)
- 拉到 0 条 → 输出 warn 但 Exit(0)（采集源更新频率低或临时不可达不算 bug）

❗本命令**不写** ``sources.yaml``，也**不写** SQLite。要落库走 ``newsbox fetch``。

测试可 monkeypatch ``_build_adapter`` 工厂，避免触发真实 HTTP。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import typer

from ...adapters.rss_adapter import RSSAdapter
from ...adapters.web_adapter import WebAdapter
from ...models import RawArticle
from .._helpers import home_option
from .._json import emit, emit_err, json_option
from ._io import find_source, load_yaml

# body 预览截断阈值（字符数）
_BODY_PREVIEW_MAX = 200


def _build_adapter(kind: str) -> Any:
    """按 kind 构造 adapter；测试可 monkeypatch 本函数返回 fake adapter。"""
    if kind == "rss":
        return RSSAdapter()
    if kind == "web":
        return WebAdapter()
    raise ValueError(f"unknown kind: {kind}")


def _format_optional(value: str | None) -> str:
    return value if value else "—"


def _format_body_preview(body: str) -> str:
    """body 预览：换行替换为 ``\\n`` 字面量；超过 _BODY_PREVIEW_MAX 字符截断。"""
    if not body:
        return ""
    # 换行替换为字面 \n（避免破版面）
    flat = body.replace("\r\n", "\n").replace("\n", "\\n")
    if len(flat) > _BODY_PREVIEW_MAX:
        return flat[:_BODY_PREVIEW_MAX] + "..."
    return flat


def _print_article(idx: int, article: RawArticle) -> None:
    """单条 RawArticle 5 字段对齐输出（idx 从 1 起）。"""
    typer.echo(f"[{idx}] title:        {article.title}")
    typer.echo(f"    url:          {article.url}")
    typer.echo(f"    external_id:  {article.external_id}")
    pub = (
        article.published_at.isoformat() if article.published_at is not None else "—"
    )
    typer.echo(f"    published_at: {pub}")
    typer.echo(f"    is_long_form: {_format_optional(article.is_long_form)}")
    typer.echo(f"    body:         {_format_body_preview(article.body)}")


def _article_to_dict(article: RawArticle) -> dict:
    """RawArticle → JSON-friendly dict（保留所有 5 字段 + body 不截断）。

    JSON 模式下不做 body 预览截断，留给消费方决定（agent 可能要全文）。
    """
    return {
        "external_id": article.external_id,
        "url": article.url,
        "title": article.title,
        "body": article.body,
        "published_at": (
            article.published_at.isoformat()
            if article.published_at is not None
            else None
        ),
        "is_long_form": article.is_long_form,
    }


def sources_test_cmd(
    source_id: str = typer.Argument(..., help="已录入的信源 id"),
    limit: int = typer.Option(3, "--limit", "-n", help="最多打印几条 RawArticle"),
    home: Path = home_option(),
    json_output: bool = json_option(),
) -> None:
    """对已录入信源试拉一次（不入库），打印前 N 条 RawArticle。

    用法::

        newsbox sources test x_dotey
        newsbox sources test anthropic_news --limit 5
    """
    yaml_path = home / "sources.yaml"
    try:
        data = load_yaml(yaml_path)
    except FileNotFoundError:
        if json_output:
            emit_err(
                f"sources.yaml not found: {yaml_path}",
                hint="run 'newsbox sources seed' first",
                path=str(yaml_path),
            )
        else:
            typer.echo(
                f"[err] sources.yaml not found: {yaml_path}\n"
                "      运行 `newsbox sources seed` 初始化",
                err=True,
            )
        raise typer.Exit(code=1)

    found = find_source(data, source_id)
    if found is None:
        if json_output:
            emit_err(f"source not found: {source_id}", id=source_id)
        else:
            typer.echo(f"[err] source not found: {source_id}", err=True)
        raise typer.Exit(code=1)

    kind, _, item = found
    adapter = _build_adapter(kind)

    # CommentedMap → 纯 dict（避免 ruamel 类型混入 adapter 测试假设）
    plain_item = dict(item)

    try:
        articles = asyncio.run(adapter.fetch(plain_item, since=None))
    except Exception as exc:  # noqa: BLE001 — 给用户看清楚 adapter 抛了什么
        if json_output:
            emit_err(
                f"adapter raised {type(exc).__name__}: {exc}",
                id=source_id,
                type=kind,
                error_type=type(exc).__name__,
            )
        else:
            typer.echo(
                f"[err] adapter raised {type(exc).__name__}: {exc}",
                err=True,
            )
        raise typer.Exit(code=1)

    total = len(articles)
    shown = min(total, max(0, limit))

    if json_output:
        emit(
            {
                "id": source_id,
                "type": kind,
                "ok": True,
                "count": total,
                "shown": shown,
                "items": [_article_to_dict(a) for a in articles[:shown]],
                "error": None,
            }
        )
        return

    if total == 0:
        typer.echo(f"[fetch] {source_id} ({kind}) → 0 articles")
        typer.echo(
            "[warn] 0 articles fetched (source 可能更新频率低或临时不可达)"
        )
        return

    typer.echo(
        f"[fetch] {source_id} ({kind}) → {total} articles, showing first {shown}"
    )
    typer.echo("")

    for i, article in enumerate(articles[:shown], start=1):
        _print_article(i, article)
        typer.echo("")

    typer.echo(f"[ok] tested {source_id}: {total} articles fetched (not persisted)")
