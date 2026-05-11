"""``newsbox teardown`` — 停容器，数据保留。

设计取舍：
- 只关 RSSHub + Redis 两个 compose 服务，**不删除任何用户数据**
  （docker-compose.yml 用绑定挂载到 ``~/.newsbox/rsshub/``，
  ``docker compose down`` 不会动绑定挂载的宿主目录）。
- 不提供 ``--purge`` 选项：避免 agent 误操作删 raw.db / 历史数据；
  用户若要彻底清理，可手工 ``rm -rf ~/.newsbox``，应明确告知后果。
- 接受 ``--home`` 用于定位 ``home/docker-compose.yml``（s6 资源化后 compose
  文件不再绑死项目根 cwd），让 pipx 安装在任意目录都能跑。
- ``--json`` 输出操作类统一 schema（s9 Step 2，详见 commands._json）。
"""
from __future__ import annotations

from pathlib import Path

import typer

from .docker_helpers import DockerError, compose_down
from ._helpers import home_option
from ._json import emit_err, emit_ok, json_option


def teardown_cmd(
    home: Path = home_option(),
    json_output: bool = json_option(),
) -> None:
    """停止容器（数据保留在 ~/.newsbox/）。"""
    compose_file = home / "docker-compose.yml"
    try:
        compose_down(compose_file)
    except DockerError as exc:
        if json_output:
            emit_err(f"teardown failed: {exc}", home=str(home))
        else:
            typer.echo(f"[err] teardown failed: {exc}", err=True)
        raise typer.Exit(code=1)
    if json_output:
        emit_ok("containers stopped, data preserved", home=str(home))
    else:
        typer.echo(f"[ok] containers stopped. data preserved at {home}/")
