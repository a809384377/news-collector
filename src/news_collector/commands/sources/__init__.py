"""``news-collector sources ...`` 子命令包。

s4-sources-management 把原 ``commands/sources_cmd.py`` 拆成 ``commands/sources/`` 包：
- ``__init__`` 暴露 ``app`` 给 cli.py 注册，并搬迁现有 seed / list 两条命令
- ``_io`` ruamel.yaml round-trip 读写底座（保留注释）
- ``_probe`` url 探测内核
- ``list_show`` / ``edit_ops`` / ``add_cmd`` / ``probe_cmd`` / ``test_cmd`` / ``export_cmd``：
  Step 5 / Step 7 由 subagent 并行实装，红线互不重叠

后续 sub-command 在各自模块里写完后，本文件 import + ``app.command(...)(fn)`` 注册即可。
"""
from __future__ import annotations

from pathlib import Path

import typer

from ... import sources as sources_module
from .._helpers import home_option
from .export_cmd import sources_export_cmd
from .list_show import sources_list_cmd, sources_show_cmd

app = typer.Typer(no_args_is_help=True, help="信源清单管理")


@app.command("seed")
def sources_seed(
    home: Path = home_option(),
    force: bool = typer.Option(False, "--force", help="覆盖已有 sources.yaml"),
) -> None:
    """把 docs/sources.seed.yaml 拷到运行时目录。"""
    target = home / "sources.yaml"
    try:
        path = sources_module.seed_sources(target, force=force)
    except FileExistsError as e:
        typer.echo(f"[err] {e}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"[ok] seeded sources.yaml → {path}")
    counts = sources_module.list_sources(path)
    summary = "  ".join(
        f"{k}:{v['enabled']}/{v['total']}" for k, v in counts.items()
    )
    typer.echo(f"      {summary}  (enabled/total)")


# Step 5 subagent A：详细 list / show / export
app.command("list")(sources_list_cmd)
app.command("show")(sources_show_cmd)
app.command("export")(sources_export_cmd)

# Step 5 subagent B：5 条改类命令注册
from . import edit_ops as _edit_ops  # noqa: E402

app.command("disable", help="禁用信源（enabled=false，幂等）")(_edit_ops.sources_disable)
app.command("enable", help="启用信源（enabled=true，幂等）")(_edit_ops.sources_enable)
app.command("remove", help="删除信源（非 --yes 需 tty 确认）")(_edit_ops.sources_remove)
app.command("edit", help="编辑信源字段（tier/domain/url/enabled）")(_edit_ops.sources_edit)
app.command("rename", help="重命名信源 id")(_edit_ops.sources_rename)

# Step 7 subagent C / D / E：probe / add / test 三命令
from .probe_cmd import sources_probe_cmd  # noqa: E402
from .add_cmd import sources_add_cmd  # noqa: E402
from .test_cmd import sources_test_cmd  # noqa: E402

app.command("probe", help="探测 URL 是否可拉取（不写 yaml）")(sources_probe_cmd)
app.command("add", help="录入信源（交互/非交互/批量三形态）")(sources_add_cmd)
app.command("test", help="对已录入信源试拉一次（不入库）")(sources_test_cmd)
