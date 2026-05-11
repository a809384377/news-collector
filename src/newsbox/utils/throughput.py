"""调用量阈值软阻断 helper（s9 Step 3 / D4）。

CLI 查询命令（一期：``read``）在执行前用 SQL ``COUNT(*)`` 预估返回行数；
超过阈值（默认 10000）时分四态决策：

- 交互 tty + 无 ``--yes`` + 无 ``--json``：stderr warn + ``typer.confirm`` 软阻断
- ``--json`` 模式：隐含 ``--yes``（不弹交互），warn 走 stderr，stdout 仍为干净 JSON
- ``--yes`` flag：stderr warn，跳过 confirm 直接执行（agent 显式覆盖路径）
- 非 tty + 无 ``--yes`` + 无 ``--json``：stderr 引导 + ``typer.Exit(1)``（沿 R-3/R-5）

调用方负责：① 用 ``count_articles_raw`` 拿预估上界 → ② 把预估 + 阈值 + 三态 flag
喂给 ``gate_read_volume`` → ③ gate 决定继续 / 阻断。
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import typer

DEFAULT_READ_WARN_THRESHOLD = 10000

# CLI→SDK 切换 snippet（warn 文案末尾附）。与 docs/sdk-usage.md 协同。
_SDK_SNIPPET = (
    "  from newsbox.sdk import read_raw\n"
    "  for art in read_raw(domain='ai', since=...): ...  # 流式不装内存"
)


def count_articles_raw(
    db_path: Path,
    *,
    domain: str = "ai",
    since: datetime | None = None,
    source_types: list[str] | None = None,
) -> int:
    """``COUNT(*) FROM articles_raw`` 按 domain / since / source_types 过滤。

    Mirror SDK ``read_raw`` 的 WHERE 子句（domain via ``json_each``、since via
    ``fetched_at``、source_types via ``IN``）。CLI 层后置 filter（``source_id`` /
    ``tier``）此处不参与计数——本返回值是实际可见数的 **上界**，作为阈值预警的
    合理近似（保守一侧：宁可多 warn 一次，不漏报）。
    """
    where: list[str] = [
        "EXISTS (SELECT 1 FROM json_each(domain_tags) WHERE value = :domain)"
    ]
    params: dict[str, object] = {"domain": domain}
    if since is not None:
        where.append("fetched_at >= :since")
        params["since"] = since.isoformat()
    if source_types:
        placeholders = []
        for i, st in enumerate(source_types):
            key = f"st{i}"
            placeholders.append(f":{key}")
            params[key] = st
        where.append(f"source_type IN ({', '.join(placeholders)})")

    sql = f"SELECT COUNT(*) FROM articles_raw WHERE {' AND '.join(where)}"
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(sql, params)
        return int(cur.fetchone()[0])
    finally:
        conn.close()


def _stdin_is_tty() -> bool:
    """单独抽出便于测试 monkeypatch 注入（沿 R-3 helper 模式）。"""
    return sys.stdin.isatty()


def gate_read_volume(
    predicted: int,
    threshold: int,
    *,
    as_json: bool,
    yes: bool,
) -> None:
    """阈值软阻断闸门。

    ``predicted <= threshold`` 直接返回；否则按四态分支处理（见模块 docstring）。

    Raises:
        typer.Exit(1): 交互模式用户答 ``no``，或非 tty 环境且无 ``--yes`` / ``--json``。
    """
    if predicted <= threshold:
        return

    warn = (
        f"[warn] 预计返回 {predicted:,} 条记录（>{threshold:,} 阈值），"
        f"CLI + jq 在此规模性能掉队；考虑切换到 SDK：\n{_SDK_SNIPPET}"
    )
    typer.echo(warn, err=True)

    if as_json or yes:
        return  # 已 warn，不阻断

    if not _stdin_is_tty():
        typer.echo(
            "[err] 非交互环境无法 confirm；加 --yes 显式继续，或改用 SDK",
            err=True,
        )
        raise typer.Exit(code=1)

    if not typer.confirm("继续？", default=False):
        raise typer.Exit(code=1)
