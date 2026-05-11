"""``sources`` 子组：5 条改类命令（disable / enable / remove / edit / rename）。

s4-sources-management Step 5 subagent B 实装。所有 yaml 操作走 ``_io`` 模块的
公开 API，**不直接 import yaml / ruamel**。

命令规格
========

- ``disable <id>``   把 ``enabled`` 改为 False（幂等）
- ``enable <id>``    把 ``enabled`` 改为 True（幂等；缺省字段视为 enabled）
- ``remove <id>``    删条目；非 ``--yes`` 时要 tty 确认
- ``edit <id>``      改 ``tier / domain / url / enabled`` 任意子集；至少传一个
- ``rename <old> <new>``  改 id；冲突或 old==new 报错

stdin tty 检测
==============
remove 命令需要在交互终端执行 typer.confirm。直接调 ``sys.stdin.isatty()`` 在
``CliRunner`` 下不可 monkeypatch（CliRunner 替换了 sys.stdin），见 KNOWLEDGE-LOG #15。
故包了一层 ``_stdin_is_tty()`` 函数，测试 monkeypatch 这个名字即可。
"""
from __future__ import annotations

import sys
from pathlib import Path

import typer

from .._helpers import home_option
from .._json import emit_err, emit_ok, json_option
from . import _io


def _stdin_is_tty() -> bool:
    """检查 stdin 是否为交互终端。

    包一层函数让测试可 monkeypatch ``edit_ops._stdin_is_tty`` 而不动 sys.stdin
    （CliRunner 替换了 sys.stdin，monkeypatch ``sys.stdin.isatty`` 失效）。
    """
    return sys.stdin.isatty()


# ----- disable / enable --------------------------------------------------


def sources_disable(
    source_id: str = typer.Argument(..., help="要禁用的信源 id"),
    home: Path = home_option(),
    json_output: bool = json_option(),
) -> None:
    """禁用信源（``enabled: false``，幂等）。"""
    yaml_path = home / "sources.yaml"
    try:
        data = _io.load_yaml(yaml_path)
    except FileNotFoundError:
        if json_output:
            emit_err(
                f"sources.yaml not found: {yaml_path}",
                path=str(yaml_path),
            )
        else:
            typer.echo(f"[err] sources.yaml not found: {yaml_path}", err=True)
        raise typer.Exit(code=1)

    # 幂等检查：先 find；旧 yaml 缺省视为 enabled，所以 already disabled 仅当
    # 字段显式 = False
    found = _io.find_source(data, source_id)
    if found is None:
        if json_output:
            emit_err(f"source not found: {source_id}", id=source_id)
        else:
            typer.echo(f"[err] source not found: {source_id}", err=True)
        raise typer.Exit(code=1)
    already = found[2].get("enabled") is False

    def mutator(item: dict) -> None:
        item["enabled"] = False

    _io.update_source(data, source_id, mutator)
    _io.save_yaml(yaml_path, data)
    if json_output:
        emit_ok(
            "source disabled",
            id=source_id,
            already=already,
            enabled=False,
        )
        return
    if already:
        typer.echo(f"[ok] disabled {source_id} (already disabled)")
    else:
        typer.echo(f"[ok] disabled {source_id}")


def sources_enable(
    source_id: str = typer.Argument(..., help="要启用的信源 id"),
    home: Path = home_option(),
    json_output: bool = json_option(),
) -> None:
    """启用信源（``enabled: true``，幂等）。

    旧 yaml 缺省视为 enabled——若条目从未声明 ``enabled`` 字段或字段=true，提示
    ``(already enabled)``。
    """
    yaml_path = home / "sources.yaml"
    try:
        data = _io.load_yaml(yaml_path)
    except FileNotFoundError:
        if json_output:
            emit_err(
                f"sources.yaml not found: {yaml_path}",
                path=str(yaml_path),
            )
        else:
            typer.echo(f"[err] sources.yaml not found: {yaml_path}", err=True)
        raise typer.Exit(code=1)

    found = _io.find_source(data, source_id)
    if found is None:
        if json_output:
            emit_err(f"source not found: {source_id}", id=source_id)
        else:
            typer.echo(f"[err] source not found: {source_id}", err=True)
        raise typer.Exit(code=1)
    # 缺省视为 enabled；only False 算 disabled
    already = found[2].get("enabled", True) is True

    def mutator(item: dict) -> None:
        item["enabled"] = True

    _io.update_source(data, source_id, mutator)
    _io.save_yaml(yaml_path, data)
    if json_output:
        emit_ok(
            "source enabled",
            id=source_id,
            already=already,
            enabled=True,
        )
        return
    if already:
        typer.echo(f"[ok] enabled {source_id} (already enabled)")
    else:
        typer.echo(f"[ok] enabled {source_id}")


# ----- remove ------------------------------------------------------------


def sources_remove(
    source_id: str = typer.Argument(..., help="要删除的信源 id"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过交互确认"),
    home: Path = home_option(),
    json_output: bool = json_option(),
) -> None:
    """从 sources.yaml 删除信源。

    非 ``--yes`` 时要求 tty 交互确认；CI / piped stdin 必须显式传 ``--yes``。
    ``--json`` 隐含 ``--yes``（agent 自动化场景跳 confirm）。
    """
    yaml_path = home / "sources.yaml"
    try:
        data = _io.load_yaml(yaml_path)
    except FileNotFoundError:
        if json_output:
            emit_err(
                f"sources.yaml not found: {yaml_path}",
                path=str(yaml_path),
            )
        else:
            typer.echo(f"[err] sources.yaml not found: {yaml_path}", err=True)
        raise typer.Exit(code=1)

    found = _io.find_source(data, source_id)
    if found is None:
        if json_output:
            emit_err(f"source not found: {source_id}", id=source_id)
        else:
            typer.echo(f"[err] source not found: {source_id}", err=True)
        raise typer.Exit(code=1)
    kind, _, item = found

    # --json 模式：隐含 --yes，跳过所有 tty / confirm 分支
    if not yes and not json_output:
        if not _stdin_is_tty():
            typer.echo(
                "[err] removal requires interactive confirmation; pass --yes",
                err=True,
            )
            raise typer.Exit(code=1)
        url = item.get("url", "")
        confirmed = typer.confirm(
            f"Remove source {source_id}? (kind={kind}, url={url})",
            default=False,
        )
        if not confirmed:
            typer.echo("[skip] removal cancelled")
            return

    _io.remove_source(data, source_id)
    _io.save_yaml(yaml_path, data)
    if json_output:
        emit_ok(
            "source removed",
            id=source_id,
            type=kind,
            url=item.get("url", ""),
        )
        return
    typer.echo(f"[ok] removed {source_id}")
    typer.echo(
        '[note] 邻近 source 的"悬空注释"可能在 ruamel 重写时丢失，可 git diff 检查'
    )


# ----- edit --------------------------------------------------------------


def sources_edit(
    source_id: str = typer.Argument(..., help="要编辑的信源 id"),
    tier: str | None = typer.Option(None, "--tier", help="覆盖 tier 字段"),
    domain: str | None = typer.Option(
        None, "--domain", help="逗号分隔，如 'ai,finance'"
    ),
    url: str | None = typer.Option(None, "--url", help="覆盖 url 字段"),
    enabled: bool | None = typer.Option(
        None, "--enabled/--disabled", help="覆盖 enabled 字段"
    ),
    home: Path = home_option(),
    json_output: bool = json_option(),
) -> None:
    """修改 source 的 ``tier / domain / url / enabled`` 字段（至少一个）。

    不允许通过 edit 改 id（要改 id 走 ``sources rename``）。
    """
    if tier is None and domain is None and url is None and enabled is None:
        if json_output:
            emit_err(
                "no field to update",
                required_one_of=["tier", "domain", "url", "enabled"],
                id=source_id,
            )
        else:
            typer.echo(
                "[err] no field to update; pass --tier / --domain / --url / --enabled",
                err=True,
            )
        raise typer.Exit(code=1)

    yaml_path = home / "sources.yaml"
    try:
        data = _io.load_yaml(yaml_path)
    except FileNotFoundError:
        if json_output:
            emit_err(
                f"sources.yaml not found: {yaml_path}",
                path=str(yaml_path),
            )
        else:
            typer.echo(f"[err] sources.yaml not found: {yaml_path}", err=True)
        raise typer.Exit(code=1)

    # 解析 domain → list
    domain_list: list[str] | None = None
    if domain is not None:
        domain_list = [d.strip() for d in domain.split(",") if d.strip()]

    applied: list[str] = []
    changes: dict[str, object] = {}

    def mutator(item: dict) -> None:
        if tier is not None:
            item["tier"] = tier
            applied.append(f"tier={tier}")
            changes["tier"] = tier
        if domain_list is not None:
            item["domain"] = domain_list
            applied.append(f"domain={domain_list}")
            changes["domain"] = list(domain_list)
        if url is not None:
            item["url"] = url
            applied.append(f"url={url}")
            changes["url"] = url
        if enabled is not None:
            item["enabled"] = enabled
            applied.append(f"enabled={enabled}")
            changes["enabled"] = enabled

    ok = _io.update_source(data, source_id, mutator)
    if not ok:
        if json_output:
            emit_err(f"source not found: {source_id}", id=source_id)
        else:
            typer.echo(f"[err] source not found: {source_id}", err=True)
        raise typer.Exit(code=1)

    _io.save_yaml(yaml_path, data)
    if json_output:
        emit_ok("source edited", id=source_id, changes=changes)
        return
    typer.echo(f"[ok] edited {source_id}: " + " ".join(applied))


# ----- rename ------------------------------------------------------------


def sources_rename(
    old_id: str = typer.Argument(..., help="原 id"),
    new_id: str = typer.Argument(..., help="新 id"),
    home: Path = home_option(),
    json_output: bool = json_option(),
) -> None:
    """重命名 source 的 id（全局唯一性由 ``_io.rename_source`` 保证）。"""
    yaml_path = home / "sources.yaml"
    try:
        data = _io.load_yaml(yaml_path)
    except FileNotFoundError:
        if json_output:
            emit_err(
                f"sources.yaml not found: {yaml_path}",
                path=str(yaml_path),
            )
        else:
            typer.echo(f"[err] sources.yaml not found: {yaml_path}", err=True)
        raise typer.Exit(code=1)

    try:
        ok = _io.rename_source(data, old_id, new_id)
    except _io.SourceIdConflictError:
        if json_output:
            emit_err(f"new id conflicts: {new_id}", old_id=old_id, new_id=new_id)
        else:
            typer.echo(f"[err] new id conflicts: {new_id}", err=True)
        raise typer.Exit(code=1)

    if not ok:
        if json_output:
            emit_err(f"source not found: {old_id}", id=old_id)
        else:
            typer.echo(f"[err] source not found: {old_id}", err=True)
        raise typer.Exit(code=1)

    _io.save_yaml(yaml_path, data)
    if json_output:
        emit_ok("source renamed", old_id=old_id, new_id=new_id)
        return
    typer.echo(f"[ok] renamed {old_id} -> {new_id}")
