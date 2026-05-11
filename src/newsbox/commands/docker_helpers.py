"""docker compose 操作的 subprocess 薄包装。

设计取舍：
- 直接调 ``docker compose ...`` CLI 而非 docker SDK Python 包，避免额外依赖
- 所有调用都带超时 + 错误规整：抛 ``DockerError`` 携带可读消息，让上层命令
  （setup/teardown/restart/doctor/status）能直接打印给用户而无需再处理 stderr
- ``container_status()`` 返回 ``{"rsshub": "Up"/"Exited"/"Missing", "redis": ...}``
  三态，Missing 表示容器没创建过；Up/Exited 与 docker compose 输出对齐
- compose 文件位置不再绑死项目根 cwd：调用方必须传 ``compose_file: Path``，
  docker compose 用 ``-f <path>`` 显式定位，运行目录无关。这让 pipx 安装的
  CLI 在任意目录都能跑 setup/teardown 等命令（s6-distribution-package）

不在本模块做的事：
- 不写"启动后等容器健康"的轮询（restart 命令在自己里加 sleep + 复查）
- 不调 docker daemon API 的健康检查；用 ``docker info`` 退出码判定即可
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


class DockerError(RuntimeError):
    """docker compose 操作失败时抛出，携带 stderr 摘要给用户。"""


def _compose_base_cmd(compose_file: Path) -> list[str]:
    """返回 ``docker compose -f <compose_file>`` 命令前缀。

    显式 ``-f`` 让命令运行目录与 yml 位置解耦，pipx 安装到 /opt 之类目录
    依旧能找对 yml。
    """
    return ["docker", "compose", "-f", str(compose_file)]


def docker_available() -> bool:
    """检查 ``docker`` CLI 是否在 PATH。不调 daemon。"""
    return shutil.which("docker") is not None


def docker_daemon_alive(timeout: float = 5.0) -> bool:
    """检查 docker daemon 是否在跑。

    使用 ``docker info`` 退出码判定。daemon 没起时 docker CLI 会返回非 0。
    """
    if not docker_available():
        return False
    try:
        r = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=timeout,
            text=True,
        )
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        return False
    except Exception:  # noqa: BLE001
        return False


def _run_compose(
    args: list[str], timeout: float, compose_file: Path
) -> subprocess.CompletedProcess[str]:
    """运行 ``docker compose -f <compose_file> <args>``；非 0 退出码抛 DockerError。"""
    if not docker_available():
        raise DockerError("docker CLI 未安装或不在 PATH")
    if not compose_file.exists():
        raise DockerError(
            f"docker-compose.yml 不存在: {compose_file}\n"
            "请运行 `newsbox setup` 自动补齐（v0.5.1 起 compose 文件存放在 home 目录）"
        )

    cmd = _compose_base_cmd(compose_file) + args
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise DockerError(
            f"docker compose {' '.join(args)} 超时（>{timeout}s）"
        ) from exc

    if r.returncode != 0:
        stderr = (r.stderr or "").strip()
        msg = f"docker compose {' '.join(args)} 失败 (exit={r.returncode})"
        if stderr:
            msg += f": {stderr.splitlines()[-1][:200]}"
        raise DockerError(msg)
    return r


def compose_up(compose_file: Path, timeout: float = 90.0) -> None:
    """``docker compose up -d``：拉镜像（如缺）+ 启容器。已 Up 则幂等。"""
    _run_compose(["up", "-d"], timeout=timeout, compose_file=compose_file)


def compose_down(compose_file: Path, timeout: float = 30.0) -> None:
    """``docker compose down``：停容器 + 移除（数据 volume 保留，因为 compose
    文件用的是绑定挂载到 ``~/.newsbox/rsshub/``）。"""
    _run_compose(["down"], timeout=timeout, compose_file=compose_file)


def compose_restart(
    compose_file: Path, service: str = "rsshub", timeout: float = 60.0
) -> None:
    """``docker compose restart <service>``：重启某容器；其他容器不动。"""
    _run_compose(
        ["restart", service], timeout=timeout, compose_file=compose_file
    )


def container_status(compose_file: Path, timeout: float = 10.0) -> dict[str, str]:
    """返回各 compose 服务的当前状态。

    使用 ``docker compose ps --format json`` 解析 — 输出每行一个 JSON 对象
    （docker compose v2 格式）。

    Returns:
        ``{"rsshub": "Up", "redis": "Up"}`` / ``"Exited"`` / ``"Missing"``
        Missing 表示该服务在 compose ps 输出中不存在（从未启过或被移除）

    Raises:
        DockerError: docker daemon 未启 / compose 调用失败 / yml 不存在
    """
    if not docker_daemon_alive(timeout=timeout):
        raise DockerError("docker daemon 未运行，无法查询容器状态")

    r = _run_compose(
        ["ps", "--format", "json", "--all"],
        timeout=timeout,
        compose_file=compose_file,
    )
    services: dict[str, str] = {"rsshub": "Missing", "redis": "Missing"}

    out = (r.stdout or "").strip()
    if not out:
        return services

    # docker compose v2 输出形式有两种：每行一个对象（NDJSON）或单个 JSON 数组。
    # 兼容两种。
    parsed: list[dict] = []
    if out.startswith("["):
        try:
            parsed = json.loads(out)
        except json.JSONDecodeError:
            parsed = []
    else:
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    for entry in parsed:
        svc = entry.get("Service") or entry.get("Name", "")
        # State 字段 docker compose v2 输出 "running" / "exited" / "created" 等
        state = (entry.get("State") or "").lower()
        if state == "running":
            mapped = "Up"
        elif state in ("exited", "dead"):
            mapped = "Exited"
        elif state in ("created", "restarting", "paused"):
            mapped = state.capitalize()
        else:
            mapped = state.capitalize() or "Unknown"

        if svc in services:
            services[svc] = mapped

    return services
