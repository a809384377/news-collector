"""``newsbox restart`` — 重启 RSSHub 容器。

设计取舍：
- 只重启 rsshub（Redis 一般无需重启，且重启 Redis 会丢 RSSHub 的 cache）；
  典型触发场景是 X token 换了 / 容器抽风 / 路由偶发卡死。
- 重启后短暂轮询（最多 5 秒）等待容器恢复 Up；时间窗内未恢复也只发 [warn]
  并 exit 0 —— 因为 ``docker compose restart`` 本身已成功下发，"还没起来"
  并不等于硬失败，建议用户跑 ``newsbox status`` 进一步看。
- 接受 ``--home`` 用于定位 ``home/docker-compose.yml``（s6 资源化后 compose
  文件不再绑死项目根 cwd），让 pipx 安装在任意目录都能跑。
- 暂不提供 ``--service`` 选项：当前唯一合理的目标就是 rsshub；
  后续若真需要扩展（如 redis）再加，避免 over-engineering。
"""
from __future__ import annotations

import time
from pathlib import Path

import typer

from .docker_helpers import DockerError, compose_restart, container_status
from ._helpers import home_option
from ._json import emit_err, emit_ok, json_option

# 等待容器恢复 Up 的总预算 / 单次轮询间隔（秒）
_WAIT_TOTAL_SECONDS = 5
_WAIT_INTERVAL_SECONDS = 1


def restart_cmd(
    home: Path = home_option(),
    json_output: bool = json_option(),
) -> None:
    """重启 RSSHub 容器（Redis 不动）。"""
    compose_file = home / "docker-compose.yml"
    try:
        compose_restart(compose_file, "rsshub")
    except DockerError as exc:
        if json_output:
            emit_err(f"restart failed: {exc}", home=str(home))
        else:
            typer.echo(f"[err] restart failed: {exc}", err=True)
        raise typer.Exit(code=1)

    # 等容器恢复 Up，最多 _WAIT_TOTAL_SECONDS 秒，每秒查一次。
    # 重启过程中 ``container_status`` 短暂窗口可能抛 DockerError（如
    # daemon 还在切换状态），允许重试，不立即报错。
    final_state = "Unknown"
    for _ in range(_WAIT_TOTAL_SECONDS):
        time.sleep(_WAIT_INTERVAL_SECONDS)
        try:
            statuses = container_status(compose_file)
            final_state = statuses.get("rsshub", "Unknown")
            if final_state == "Up":
                break
        except DockerError:
            continue

    if final_state == "Up":
        if json_output:
            emit_ok(
                "rsshub restarted",
                home=str(home),
                service="rsshub",
                state=final_state,
            )
        else:
            typer.echo(f"[ok] rsshub restarted (state={final_state})")
    else:
        if json_output:
            # 重启命令已下发但容器未在窗口内恢复 Up —— 还是 ok=true（重启动作成功），
            # 但 details 里带 warn=True 让消费方判断是否需要进一步操作。
            emit_ok(
                f"rsshub restart issued but state={final_state} after "
                f"{_WAIT_TOTAL_SECONDS}s; check 'newsbox status'",
                home=str(home),
                service="rsshub",
                state=final_state,
                warn=True,
                waited_seconds=_WAIT_TOTAL_SECONDS,
            )
        else:
            typer.echo(
                f"[warn] rsshub restart issued but state={final_state} after "
                f"{_WAIT_TOTAL_SECONDS}s; check 'newsbox status'"
            )
