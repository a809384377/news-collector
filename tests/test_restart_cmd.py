"""``newsbox restart`` 命令测试。

覆盖：
1. compose_restart 成功 + 容器立即 Up → exit 0 + "rsshub restarted (state=Up)"
2. compose_restart 抛 DockerError → exit 1 + "restart failed"
3. compose_restart 成功但容器 5s 内未恢复 Up → exit 0 + [warn] + state=Restarting
4. compose_restart 成功 + 容器在第 3 次轮询时恢复 Up → exit 0 + [ok]

所有 docker / sleep 都被 mock，不真调 docker、不真等。
"""
from __future__ import annotations

import json
from typing import Any

import typer
from typer.testing import CliRunner

import newsbox.commands.restart as restart_module
from newsbox.commands.docker_helpers import DockerError
from newsbox.commands.restart import restart_cmd


# ---- helpers ---------------------------------------------------------------


def _build_app() -> typer.Typer:
    """单命令 typer app。

    typer 在只挂一个命令时会自动「扁平化」（命令变成根入口），CLI 上调用时
    无需再写命令名。挂一个 hidden 占位命令就能保持 ``restart`` 子命令形态；
    但本测试沿用扁平化模式简化 invoke，无需占位。
    """
    app = typer.Typer()
    app.command("restart")(restart_cmd)
    # 加一个 hidden 占位命令，避免 typer 单命令扁平化（保持 "restart" 作为子命令）
    app.command("_placeholder", hidden=True)(lambda: None)
    return app


def _patch_no_sleep(monkeypatch: Any) -> None:
    """让 ``time.sleep`` 不真等。

    restart.py 是 ``import time`` + ``time.sleep(...)``，所以 patch 模块属性
    ``newsbox.commands.restart.time.sleep``。
    """
    monkeypatch.setattr(restart_module.time, "sleep", lambda *a, **kw: None)


def _run(app: typer.Typer, *args: str) -> Any:
    runner = CliRunner()
    return runner.invoke(app, ["restart", *args])


# ---- tests -----------------------------------------------------------------


def test_restart_success_immediate(monkeypatch: Any) -> None:
    """compose_restart OK + container_status 第一次轮询就返回 Up。"""
    _patch_no_sleep(monkeypatch)

    calls: list[str] = []

    # s6 之后签名是 ``compose_restart(compose_file, service="rsshub", timeout=60.0)``，
    # restart_cmd 用 ``compose_restart(compose_file, "rsshub")`` 位置传参，
    # 所以 fake 第一参是 compose_file，第二参是 service。
    def _fake_compose_restart(
        compose_file: Any, service: str = "rsshub", timeout: float = 60.0
    ) -> None:
        calls.append(service)

    monkeypatch.setattr(restart_module, "compose_restart", _fake_compose_restart)
    monkeypatch.setattr(
        restart_module,
        "container_status",
        lambda *a, **kw: {"rsshub": "Up", "redis": "Up"},
    )

    result = _run(_build_app())

    assert result.exit_code == 0, result.output
    assert calls == ["rsshub"]
    assert "rsshub restarted (state=Up)" in result.output
    assert "[ok]" in result.output


def test_restart_compose_error(monkeypatch: Any) -> None:
    """compose_restart 抛 DockerError → exit 1，不再轮询 status。"""
    _patch_no_sleep(monkeypatch)

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise DockerError("daemon not running")

    monkeypatch.setattr(restart_module, "compose_restart", _boom)

    # 一旦 compose_restart 失败，container_status 不应被调用
    def _should_not_call(*args: Any, **kwargs: Any) -> dict[str, str]:
        raise AssertionError("container_status 不应在 compose_restart 失败后被调用")

    monkeypatch.setattr(restart_module, "container_status", _should_not_call)

    result = _run(_build_app())

    assert result.exit_code == 1, result.output
    assert "restart failed" in result.output
    assert "daemon not running" in result.output
    assert "[err]" in result.output


def test_restart_warning_when_not_up_after_wait(monkeypatch: Any) -> None:
    """compose_restart OK 但容器 5s 内一直 Restarting → exit 0 + [warn]。"""
    _patch_no_sleep(monkeypatch)

    monkeypatch.setattr(restart_module, "compose_restart", lambda *a, **kw: None)

    poll_counts = {"n": 0}

    def _stuck_status(*args: Any, **kwargs: Any) -> dict[str, str]:
        poll_counts["n"] += 1
        return {"rsshub": "Restarting", "redis": "Up"}

    monkeypatch.setattr(restart_module, "container_status", _stuck_status)

    result = _run(_build_app())

    assert result.exit_code == 0, result.output
    assert "[warn]" in result.output
    assert "state=Restarting" in result.output
    assert "after 5s" in result.output
    assert "newsbox status" in result.output
    # 应轮询满 5 次（_WAIT_TOTAL_SECONDS）
    assert poll_counts["n"] == restart_module._WAIT_TOTAL_SECONDS


def test_restart_recovers_within_wait(monkeypatch: Any) -> None:
    """compose_restart OK + 前 2 次 Restarting，第 3 次 Up → exit 0 + [ok]。"""
    _patch_no_sleep(monkeypatch)

    monkeypatch.setattr(restart_module, "compose_restart", lambda *a, **kw: None)

    states = iter(
        [
            {"rsshub": "Restarting", "redis": "Up"},
            {"rsshub": "Restarting", "redis": "Up"},
            {"rsshub": "Up", "redis": "Up"},
            # 多余的 fallback：理论上不会被取，给 iter 留余量更稳
            {"rsshub": "Up", "redis": "Up"},
            {"rsshub": "Up", "redis": "Up"},
        ]
    )

    monkeypatch.setattr(
        restart_module,
        "container_status",
        lambda *a, **kw: next(states),
    )

    result = _run(_build_app())

    assert result.exit_code == 0, result.output
    assert "[ok]" in result.output
    assert "rsshub restarted (state=Up)" in result.output


def test_restart_json_success(monkeypatch: Any) -> None:
    """--json 模式 + 容器立即 Up → ok=true，service=rsshub，state=Up。"""
    _patch_no_sleep(monkeypatch)
    monkeypatch.setattr(restart_module, "compose_restart", lambda *a, **kw: None)
    monkeypatch.setattr(
        restart_module,
        "container_status",
        lambda *a, **kw: {"rsshub": "Up", "redis": "Up"},
    )

    result = _run(_build_app(), "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["message"] == "rsshub restarted"
    assert payload["details"]["service"] == "rsshub"
    assert payload["details"]["state"] == "Up"


def test_restart_json_compose_error(monkeypatch: Any) -> None:
    """--json 模式 + compose_restart 抛 DockerError → ok=false + exit 1。"""
    _patch_no_sleep(monkeypatch)

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise DockerError("daemon not running")

    monkeypatch.setattr(restart_module, "compose_restart", _boom)

    result = _run(_build_app(), "--json")

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert "restart failed" in payload["message"]
    assert "daemon not running" in payload["message"]


def test_restart_tolerates_transient_dockererror_then_recovers(
    monkeypatch: Any,
) -> None:
    """额外用例：轮询过程中 container_status 偶发 DockerError 不应使命令失败。

    第 1 次轮询抛 DockerError（模拟 daemon 短暂窗口），第 2 次返回 Up。
    """
    _patch_no_sleep(monkeypatch)

    monkeypatch.setattr(restart_module, "compose_restart", lambda *a, **kw: None)

    call_n = {"n": 0}

    def _flaky_status(*args: Any, **kwargs: Any) -> dict[str, str]:
        call_n["n"] += 1
        if call_n["n"] == 1:
            raise DockerError("daemon transient")
        return {"rsshub": "Up", "redis": "Up"}

    monkeypatch.setattr(restart_module, "container_status", _flaky_status)

    result = _run(_build_app())

    assert result.exit_code == 0, result.output
    assert "[ok]" in result.output
    assert "rsshub restarted (state=Up)" in result.output
