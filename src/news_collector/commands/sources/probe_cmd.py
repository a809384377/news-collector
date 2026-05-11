"""``news-collector sources probe`` 子命令实装。

s4-sources-management Step 7 subagent C 产出。

设计要点
========
- 单 url / 批量 ``--from-file`` 双形态共用一个 typer 命令；位置参数与选项互斥
- probe 命令**只读不写**：探测 url，输出 7 字段（单条）或 4 列表格（批量），
  不动 sources.yaml。落 yaml 是后续 ``sources add`` 的事
- 批量模式共享一个 ``httpx.AsyncClient``：避免每条 url 各自开 connection pool
  （s2 KNOWLEDGE R-2 提示我们不要为不存在的并发问题过度设计，串行已够快）
- 退出码：单条 reachable=False / 批量任一失败 → 1，否则 0；让 shell 脚本能捕获
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import typer

from ._probe import ProbeResult, probe

# 批量表格 URL 列截断阈值
_URL_DISPLAY_MAX = 60
# 单条详情字段宽度（左列对齐）
_LABEL_WIDTH = 14


def _truncate_url(url: str, limit: int = _URL_DISPLAY_MAX) -> str:
    if not isinstance(url, str):
        url = "" if url is None else str(url)
    if len(url) <= limit:
        return url
    # 截到 limit-3 + ... 总宽度恰好 = limit
    return url[: limit - 3] + "..."


def _fmt_status(result: ProbeResult) -> str:
    """批量表 STATUS 列：HTTP 数字 / ERR / —。"""
    if result.status_code is not None:
        return str(result.status_code)
    return "ERR"


def _fmt_type(result: ProbeResult) -> str:
    return result.source_type or "—"


def _fmt_optional(value: str | None) -> str:
    return value if value else "—"


def _read_url_file(path: Path) -> list[str]:
    """读批量 url 文件：去空白、跳空行、跳 ``#`` 开头注释行。"""
    if not path.exists():
        raise typer.BadParameter(f"--from-file 路径不存在: {path}")
    raw = path.read_text(encoding="utf-8")
    urls: list[str] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        urls.append(s)
    return urls


async def _probe_one(url: str) -> ProbeResult:
    """单 url 探测：构造一次性 client（probe 内部已 own）。"""
    return await probe(url)


async def _probe_many(urls: list[str]) -> list[ProbeResult]:
    """批量探测：共享一个 client；串行（IO 瓶颈，并发收益不抵设计成本）。"""
    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
        return [await probe(url, client=client) for url in urls]


def _print_single(result: ProbeResult) -> None:
    """单 url 7 字段彩色对齐输出（彩色用 typer.secho）。"""
    rows: list[tuple[str, str, str | None]] = [
        ("URL", result.url, None),
        ("reachable", "yes" if result.reachable else "no", "green" if result.reachable else "red"),
        ("status_code", _fmt_optional(str(result.status_code) if result.status_code is not None else None), None),
        ("source_type", _fmt_optional(result.source_type), None),
        ("suggested_id", _fmt_optional(result.suggested_id), None),
        ("sample_title", _fmt_optional(result.sample_title), None),
        ("error", _fmt_optional(result.error), "red" if result.error else None),
    ]
    for label, value, color in rows:
        line = f"{label:<{_LABEL_WIDTH}} {value}"
        if color:
            typer.secho(line, fg=color)
        else:
            typer.echo(line)


def _print_batch(results: list[ProbeResult]) -> None:
    """批量 4 列表格输出 + 末尾汇总。"""
    # 列宽自适应：URL 截断后最大宽度（含表头）；STATUS / TYPE / SUGGESTED_ID 同理
    url_cells = [_truncate_url(r.url) for r in results]
    status_cells = [_fmt_status(r) for r in results]
    type_cells = [_fmt_type(r) for r in results]
    sid_cells = [_fmt_optional(r.suggested_id) for r in results]

    url_w = max([len("URL")] + [len(c) for c in url_cells])
    status_w = max([len("STATUS")] + [len(c) for c in status_cells])
    type_w = max([len("TYPE")] + [len(c) for c in type_cells])
    sid_w = max([len("SUGGESTED_ID")] + [len(c) for c in sid_cells])

    header = (
        f"{'URL':<{url_w}}  "
        f"{'STATUS':<{status_w}}  "
        f"{'TYPE':<{type_w}}  "
        f"{'SUGGESTED_ID':<{sid_w}}"
    )
    typer.echo(header)
    for url_cell, status_cell, type_cell, sid_cell in zip(
        url_cells, status_cells, type_cells, sid_cells
    ):
        typer.echo(
            f"{url_cell:<{url_w}}  "
            f"{status_cell:<{status_w}}  "
            f"{type_cell:<{type_w}}  "
            f"{sid_cell:<{sid_w}}"
        )

    total = len(results)
    ok = sum(1 for r in results if r.reachable)
    fail = total - ok
    typer.echo("---")
    typer.echo(f"{ok}/{total} reachable; {fail}/{total} unreachable")


def sources_probe_cmd(
    url: str = typer.Argument(
        None,
        help="要探测的 URL（与 --from-file 互斥）",
    ),
    from_file: Path = typer.Option(
        None,
        "--from-file",
        help="批量探测：每行一条 URL（空行 / # 开头注释行会被跳过）",
    ),
) -> None:
    """探测 URL 是否可拉取（HTTP + 类型猜测 + 1 条样本标题）。

    单 url 模式：``sources probe https://example.com``
    批量模式：``sources probe --from-file=/path/to/urls.txt``

    本命令**不写** sources.yaml；要落库请用 ``sources add``。
    """
    # 互斥校验
    if url is None and from_file is None:
        raise typer.BadParameter("must pass <url> or --from-file")
    if url is not None and from_file is not None:
        raise typer.BadParameter("--from-file 与位置参数 url 互斥，二选一")

    if from_file is not None:
        urls = _read_url_file(from_file)
        if not urls:
            typer.echo(
                f"[err] {from_file} 没有有效 URL（空文件 / 全注释行）",
                err=True,
            )
            raise typer.Exit(code=1)
        results = asyncio.run(_probe_many(urls))
        _print_batch(results)
        # 任一不可达则退码 1
        if any(not r.reachable for r in results):
            raise typer.Exit(code=1)
        return

    # 单 url 模式
    result = asyncio.run(_probe_one(url))
    _print_single(result)
    if not result.reachable:
        raise typer.Exit(code=1)
