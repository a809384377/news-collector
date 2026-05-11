"""``newsbox config ...`` 子命令。

- ``init`` 是操作类：``--json`` 用 ``emit_ok / emit_err``。``already_exists`` 字段在失败
  分支里标记目标已存在，agent 可据此判断是否需要 ``--force``。
- ``show`` 是信息类：``--json`` 直接序列化 AppConfig dump + home/path 元数据。
  人类视图保留原 ``json.dumps + secrets`` 两行格式。
"""
from __future__ import annotations

import json
from pathlib import Path

import typer

from .. import config as config_module
from ._helpers import home_option, load_app_config
from ._json import emit, emit_err, emit_ok, json_option

app = typer.Typer(no_args_is_help=True, help="配置文件管理")


@app.command("init")
def config_init(
    home: Path = home_option(),
    force: bool = typer.Option(False, "--force", help="覆盖已有 config.yaml"),
    json_output: bool = json_option(),
) -> None:
    """把默认 config.yaml 写到运行时目录。"""
    try:
        path = config_module.write_default_config(home, force=force)
    except FileExistsError as e:
        if json_output:
            emit_err(
                f"{e}",
                home=str(home),
                path=str(home / "config.yaml"),
                already_exists=True,
            )
        else:
            typer.echo(f"[err] {e}", err=True)
        raise typer.Exit(code=1)
    if json_output:
        emit_ok(
            "config initialized",
            home=str(home),
            path=str(path),
            already_exists=False,
        )
    else:
        typer.echo(f"[ok] config initialized: {path}")


@app.command("show")
def config_show(
    home: Path = home_option(),
    json_output: bool = json_option(),
) -> None:
    """加载并显示当前配置（密钥已脱敏）。"""
    cfg = load_app_config(home)
    body = cfg.model_dump(exclude={"secrets"})
    if json_output:
        emit(
            {
                "home": str(home),
                "path": str(home / "config.yaml"),
                "config": body,
                "secrets": repr(cfg.secrets),
            }
        )
        return
    typer.echo(json.dumps(body, indent=2, ensure_ascii=False))
    typer.echo(f"secrets: {cfg.secrets!r}")
