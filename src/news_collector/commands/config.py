"""``news-collector config ...`` 子命令。"""
from __future__ import annotations

import json
from pathlib import Path

import typer

from .. import config as config_module
from ._helpers import home_option, load_app_config

app = typer.Typer(no_args_is_help=True, help="配置文件管理")


@app.command("init")
def config_init(
    home: Path = home_option(),
    force: bool = typer.Option(False, "--force", help="覆盖已有 config.yaml"),
) -> None:
    """把默认 config.yaml 写到运行时目录。"""
    try:
        path = config_module.write_default_config(home, force=force)
    except FileExistsError as e:
        typer.echo(f"[err] {e}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"[ok] config initialized: {path}")


@app.command("show")
def config_show(home: Path = home_option()) -> None:
    """加载并显示当前配置（密钥已脱敏）。"""
    cfg = load_app_config(home)
    body = cfg.model_dump(exclude={"secrets"})
    typer.echo(json.dumps(body, indent=2, ensure_ascii=False))
    typer.echo(f"secrets: {cfg.secrets!r}")
