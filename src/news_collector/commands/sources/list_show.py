"""``news-collector sources list`` / ``show`` 子命令实装。

s4-sources-management Step 5 subagent A 产出。读类，无副作用，不依赖 _probe。

设计要点
========
- list：详细表格输出，列宽自适应，URL 截断 50 字符；末尾汇总行
- show：单条完整字段，缩进 yaml 风格（保留 ruamel inline list/dict 形式）
- enabled 字段缺省视为 true（与 ``news_collector.sources`` 已有约定一致）
"""
from __future__ import annotations

import io
from pathlib import Path

import typer
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from .._helpers import home_option
from ._io import SOURCE_KINDS, find_source, load_yaml


def _to_plain(value):
    """剥离 ruamel ``CommentedMap`` / ``CommentedSeq`` 的注释附属，转 plain dict/list。

    show 命令只展示字段值本身，不应回带任何文件级注释（否则会把整段邻居注释
    一并 dump 出来）。这里递归构造 ``dict`` / ``list`` 复制结构与值，丢弃注释 metadata。
    """
    if isinstance(value, (CommentedMap, dict)):
        return {k: _to_plain(v) for k, v in value.items()}
    if isinstance(value, (CommentedSeq, list)):
        return [_to_plain(v) for v in value]
    return value

# URL 截断显示长度（list 表格用）
_URL_DISPLAY_MAX = 50


def _is_enabled(item: dict) -> bool:
    """enabled 缺省视为 true。"""
    return bool(item.get("enabled", True))


def _truncate_url(url: str, limit: int = _URL_DISPLAY_MAX) -> str:
    if not isinstance(url, str):
        url = "" if url is None else str(url)
    if len(url) <= limit:
        return url
    # 截到 limit-3 然后加 ... 保证总宽度恰好 limit
    return url[: limit - 3] + "..."


def _collect_rows(
    data: CommentedMap,
    type_filter: str | None,
    tier_filter: str | None,
    enabled_only: bool,
    disabled_only: bool,
) -> list[tuple[str, str, str, bool, str]]:
    """扫一遍 data，返回过滤后的 ``(type, tier, id, enabled, url)`` 元组列表。"""
    rows: list[tuple[str, str, str, bool, str]] = []
    for kind in SOURCE_KINDS:
        if type_filter is not None and kind != type_filter:
            continue
        items = data.get(kind) or []
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            tier = str(item.get("tier", "") or "")
            if tier_filter is not None and tier != tier_filter:
                continue
            enabled = _is_enabled(item)
            if enabled_only and not enabled:
                continue
            if disabled_only and enabled:
                continue
            src_id = str(item.get("id", "") or "")
            url = str(item.get("url", "") or "")
            rows.append((kind, tier, src_id, enabled, url))
    return rows


def _summary_line(data: CommentedMap, rows: list[tuple]) -> str:
    """构造表尾汇总行：``total=N enabled=M (rss=a/b web=c/d)``。

    汇总按"过滤后行集"统计 total / enabled，分类计数也基于过滤后的行（避免与
    展示列不一致让用户困惑）。
    """
    total = len(rows)
    enabled_total = sum(1 for r in rows if r[3])
    parts: list[str] = []
    for kind in SOURCE_KINDS:
        kind_rows = [r for r in rows if r[0] == kind]
        kind_total = len(kind_rows)
        kind_enabled = sum(1 for r in kind_rows if r[3])
        parts.append(f"{kind}={kind_enabled}/{kind_total}")
    return f"total={total} enabled={enabled_total} ({' '.join(parts)})"


def sources_list_cmd(
    home: Path = home_option(),
    type_filter: str = typer.Option(
        None,
        "--type",
        help="按类型过滤（rss / web）",
    ),
    tier_filter: str = typer.Option(
        None,
        "--tier",
        help="按 tier 字段精确过滤（如 kol / official_first_party）",
    ),
    enabled_only: bool = typer.Option(
        False,
        "--enabled-only",
        help="只显示 enabled=true 的条目（缺省字段视为 true）",
    ),
    disabled_only: bool = typer.Option(
        False,
        "--disabled-only",
        help="只显示 enabled=false 的条目",
    ),
) -> None:
    """详细列出 sources.yaml 条目（type/tier/id/enabled/url）。"""
    if enabled_only and disabled_only:
        raise typer.BadParameter(
            "--enabled-only 与 --disabled-only 互斥，不能同时使用"
        )
    if type_filter is not None and type_filter not in SOURCE_KINDS:
        raise typer.BadParameter(
            f"--type 必须是 {SOURCE_KINDS} 之一，got: {type_filter!r}"
        )

    yaml_path = home / "sources.yaml"
    try:
        data = load_yaml(yaml_path)
    except FileNotFoundError:
        typer.echo(
            f"[err] sources.yaml 不存在: {yaml_path}\n"
            f"      请先 `news-collector sources seed`",
            err=True,
        )
        raise typer.Exit(code=1)

    rows = _collect_rows(
        data,
        type_filter=type_filter,
        tier_filter=tier_filter,
        enabled_only=enabled_only,
        disabled_only=disabled_only,
    )

    # 列宽自适应：type/tier/id 取实际最大宽度（含表头）
    type_w = max([len("TYPE")] + [len(r[0]) for r in rows])
    tier_w = max([len("TIER")] + [len(r[1]) for r in rows])
    id_w = max([len("ID")] + [len(r[2]) for r in rows])
    en_w = len("EN")  # 固定 2

    header = (
        f"{'TYPE':<{type_w}} "
        f"{'TIER':<{tier_w}} "
        f"{'ID':<{id_w}} "
        f"{'EN':<{en_w}} "
        f"URL"
    )
    typer.echo(header)
    for kind, tier, src_id, enabled, url in rows:
        mark = "✓" if enabled else "✗"
        typer.echo(
            f"{kind:<{type_w}} "
            f"{tier:<{tier_w}} "
            f"{src_id:<{id_w}} "
            f"{mark:<{en_w}} "
            f"{_truncate_url(url)}"
        )
    typer.echo("---")
    typer.echo(_summary_line(data, rows))


# ------------------- show ------------------------


def _dump_value(value) -> str:
    """把单字段值序列化成一行 yaml 表达式。

    先调 ``_to_plain`` 剥离 ruamel 的注释附属，再 dump 纯结构——避免把
    sources.yaml 文件里的邻居/段尾注释一并打印出来（s4-sources-management
    Step 7 实测发现的 bug：show simonw_blog 时 domain 字段连带 web 段开头的注释）。

    list/dict 用 flow 风格输出（``[ai]``）；scalar 输出原值；末尾 doc end
    ``...`` 由调用方剥掉。
    """
    plain = _to_plain(value)
    y = YAML(typ="rt")
    y.default_flow_style = None
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    buf = io.StringIO()
    y.dump(plain, buf)
    text = buf.getvalue()
    # ruamel 在 dump scalar 时会附 doc end "...\n"；list/dict 不会
    if text.endswith("...\n"):
        text = text[:-4]
    return text.rstrip("\n")


def sources_show_cmd(
    source_id: str = typer.Argument(..., help="信源 id（跨 rss/web 唯一）"),
    home: Path = home_option(),
) -> None:
    """显示单条信源完整字段。"""
    yaml_path = home / "sources.yaml"
    try:
        data = load_yaml(yaml_path)
    except FileNotFoundError:
        typer.echo(
            f"[err] sources.yaml 不存在: {yaml_path}\n"
            f"      请先 `news-collector sources seed`",
            err=True,
        )
        raise typer.Exit(code=1)

    found = find_source(data, source_id)
    if found is None:
        typer.echo(f"[err] source not found: {source_id}", err=True)
        raise typer.Exit(code=1)

    kind, idx, item = found
    typer.echo(f"[type]  {kind}")
    typer.echo(f"[index] {idx}")
    typer.echo("---")

    # id 永远第一行；其他字段保持 yaml 中原顺序，跳过已输出的 id
    typer.echo(f"id: {item.get('id', source_id)}")
    has_enabled = False
    for key, value in item.items():
        if key == "id":
            continue
        if key == "enabled":
            has_enabled = True
        typer.echo(f"{key}: {_dump_value(value)}")
    if not has_enabled:
        # 显式补一行说明缺省状态
        typer.echo("enabled: true (默认)")
