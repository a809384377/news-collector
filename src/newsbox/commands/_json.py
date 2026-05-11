"""统一 ``--json`` 输出 helper（s9 Step 2 引入）。

两类命令、两种 schema：

1. **信息查询类**（list / show / state / status / doctor / stats / sources list 等）
   - 直接序列化命令自定义 payload；schema 由命令决定
   - 入口：``emit(payload)``

2. **操作命令类**（setup / teardown / restart / fetch / clean / sources add 等）
   - 统一 ``{ok: bool, message?: str, details?: object}`` schema
   - 入口：``emit_ok(message, **details)`` / ``emit_err(message, **details)``

设计约束：
- 所有 JSON 输出走 **stdout**（agent 管道安全）；warn / 人类可读错误仍走 stderr
- ``emit_err`` 不抛 ``typer.Exit``，调用方决定 exit code
- JSON 用 ``indent=2 / ensure_ascii=False / default=str``，便于人/agent 双方读

标准 ``--json`` flag 由 ``json_option()`` 提供，所有命令统一 ``--json`` 字面量。
"""
from __future__ import annotations

import json
from typing import Any

import typer


def json_option() -> bool:
    """标准 ``--json`` flag 工厂；调用时返回 ``typer.Option`` 对象。

    用法::

        def my_cmd(json_output: bool = json_option()) -> None: ...
    """
    return typer.Option(  # type: ignore[return-value]
        False,
        "--json",
        help="输出结构化 JSON（默认人类可读）",
    )


def _dump(payload: Any) -> None:
    typer.echo(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def emit(payload: Any) -> None:
    """信息查询类：直接序列化 payload 到 stdout（``indent=2`` 整块输出）。"""
    _dump(payload)


def emit_ndjson(items: Any) -> None:
    """流式列表：每行一条 JSON（NDJSON / JSON Lines）。

    适合可能很大的数据集（``read`` / ``sources list`` 等），agent 可用
    ``jq -c`` 或 ``while read line`` 流式消费，免读全量。
    """
    for item in items:
        typer.echo(json.dumps(item, ensure_ascii=False, default=str))


def emit_ok(message: str | None = None, **details: Any) -> None:
    """操作类成功：``{ok: true, message?, details?}`` → stdout。"""
    payload: dict[str, Any] = {"ok": True}
    if message is not None:
        payload["message"] = message
    if details:
        payload["details"] = details
    _dump(payload)


def emit_err(message: str, **details: Any) -> None:
    """操作类错误：``{ok: false, message, details?}`` → stdout。

    不抛 ``typer.Exit``；调用方按需 ``raise typer.Exit(code=N)``。
    """
    payload: dict[str, Any] = {"ok": False, "message": message}
    if details:
        payload["details"] = details
    _dump(payload)
