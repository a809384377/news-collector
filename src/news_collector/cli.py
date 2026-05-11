"""typer CLI 入口（注册壳）。

各命令实现在 ``commands/`` 子模块。本文件只负责创建顶层 ``app`` + 装载子 app
+ 注册顶层命令，不放具体业务逻辑。

注册关系::

    app
    ├── setup     ← commands.setup.setup_cmd（顶层；一键装机）
    ├── teardown  ← commands.teardown.teardown_cmd（顶层；停容器）
    ├── restart   ← commands.restart.restart_cmd（顶层；重启 rsshub）
    ├── doctor    ← commands.doctor.doctor_cmd（顶层；健康自检）
    ├── status    ← commands.status.status_cmd（顶层；综合状态）
    ├── state     ← commands.state.state_cmd（顶层；source_state 表）
    ├── logs      ← commands.logs.logs_cmd（顶层；日志尾部）
    ├── fetch     ← commands.fetch.fetch_cmd（顶层；采集编排）
    ├── read      ← commands.read.read_cmd（顶层；s5 数据视图：rich Table / NDJSON）
    ├── stats     ← commands.stats.stats_cmd（顶层；s5 数据视图：4 panel / JSON）
    ├── clean     ← commands.clean.clean_cmd（顶层；s5 数据维护：dry-run + --yes + VACUUM）
    ├── config    ← commands.config.app（子组：init / show）
    └── sources   ← commands.sources.app（子组：seed / list / + s4 扩展 13 命令）

注：``db init`` 子命令在 s3-cli-onboarding Step 5 后已合并进 ``setup``，
不再单独暴露（DECISIONS.md D3 / BRIEF 成功标准 #8）。
"""
from __future__ import annotations

import typer

from . import __version__
from .commands import clean as clean_cmd_module
from .commands import config as config_cmd
from .commands import doctor as doctor_cmd_module
from .commands import fetch as fetch_cmd_module
from .commands import logs as logs_cmd_module
from .commands import read as read_cmd_module
from .commands import restart as restart_cmd_module
from .commands import setup as setup_cmd_module
from .commands import sources as sources_cmd
from .commands import state as state_cmd_module
from .commands import stats as stats_cmd_module
from .commands import status as status_cmd_module
from .commands import teardown as teardown_cmd_module

app = typer.Typer(
    no_args_is_help=True,
    help="news-collector — 采集层基础服务",
    add_completion=False,
)

# 子命令组
app.add_typer(config_cmd.app, name="config")
app.add_typer(sources_cmd.app, name="sources")

# 顶层命令
app.command("setup")(setup_cmd_module.setup_cmd)
app.command("teardown")(teardown_cmd_module.teardown_cmd)
app.command("restart")(restart_cmd_module.restart_cmd)
app.command("doctor")(doctor_cmd_module.doctor_cmd)
app.command("status")(status_cmd_module.status_cmd)
app.command("state")(state_cmd_module.state_cmd)
app.command("logs")(logs_cmd_module.logs_cmd)
app.command("fetch")(fetch_cmd_module.fetch_cmd)
app.command("read")(read_cmd_module.read_cmd)
app.command("stats")(stats_cmd_module.stats_cmd)
app.command("clean")(clean_cmd_module.clean_cmd)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"news-collector {__version__}")
        raise typer.Exit()


@app.callback()
def root(
    version: bool = typer.Option(
        None,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="显示版本号并退出",
    ),
) -> None:
    """根回调，仅承载全局选项。"""


if __name__ == "__main__":
    app()
