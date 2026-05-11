"""``newsbox teardown`` 命令测试。

覆盖：
1. compose_down 成功 → exit 0 + 友好提示（含 "containers stopped" + "data preserved"）
2. compose_down 抛 DockerError → exit 1 + 错误消息透传
3. compose_down 被调一次（不是 0 次也不是多次）

所有用例都 mock subprocess 层（直接替换 teardown 模块里的 ``compose_down``
符号引用），不触发真实 docker 调用，CI 无 docker 也能跑。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

# 注意：别名不能用 ``td_mod`` —— pytest 会把模块级 ``td_mod``
# 当成 xunit-style teardown hook 去调，导致 AttributeError。改用 ``td_mod``。
import newsbox.commands.teardown as td_mod
from newsbox.commands.docker_helpers import DockerError
from newsbox.commands.teardown import teardown_cmd


# ---- helpers ---------------------------------------------------------------


def _build_app() -> typer.Typer:
    """构造单命令 typer app。

    typer 在只挂一个命令时会自动「扁平化」（命令直接当根命令调用）。
    挂一个隐藏占位命令强制保留 group 模式，invoke 时 args 第一项为 ``teardown``。
    与 test_state_cmd.py 不同 —— 后者直接吃扁平化模式（args 不带命令名）。
    本测试两种模式都可以；这里选 group 模式更显式。
    """
    app = typer.Typer()
    app.command("teardown")(teardown_cmd)
    app.command("_placeholder", hidden=True)(lambda: None)
    return app


def _run(app: typer.Typer, *args: str) -> Any:
    runner = CliRunner()
    return runner.invoke(app, ["teardown", *args])


# ---- tests -----------------------------------------------------------------


def test_teardown_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """compose_down 不抛错 → exit 0 + 输出含成功提示。"""
    monkeypatch.setattr(
        td_mod,
        "compose_down",
        lambda *args, **kwargs: None,
    )

    result = _run(_build_app(), "--home", str(tmp_path))

    assert result.exit_code == 0
    assert "containers stopped" in result.output
    assert "data preserved" in result.output
    # s6 之后输出格式是 ``data preserved at {home}/``，home 由 --home 决定
    assert f"{tmp_path}/" in result.output


def test_teardown_docker_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """compose_down 抛 DockerError → exit 1 + 错误消息透传。"""
    err_msg = "docker daemon 未运行"

    def fake_compose_down(*args: Any, **kwargs: Any) -> None:
        raise DockerError(err_msg)

    monkeypatch.setattr(td_mod, "compose_down", fake_compose_down)

    result = _run(_build_app(), "--home", str(tmp_path))

    assert result.exit_code == 1
    # CliRunner 默认把 stderr 合到 output；用 output 兼容
    assert "teardown failed" in result.output
    assert err_msg in result.output


def test_teardown_called_compose_down_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """成功路径下 compose_down 必须被调用且仅调用一次。"""
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def fake_compose_down(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))

    monkeypatch.setattr(td_mod, "compose_down", fake_compose_down)

    result = _run(_build_app(), "--home", str(tmp_path))

    assert result.exit_code == 0
    assert len(calls) == 1


def test_teardown_json_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--json`` 成功 → ``{ok: true, message, details: {home}}``。"""
    import json as _json

    monkeypatch.setattr(td_mod, "compose_down", lambda *a, **kw: None)

    result = _run(_build_app(), "--home", str(tmp_path), "--json")

    assert result.exit_code == 0
    parsed = _json.loads(result.output)
    assert parsed["ok"] is True
    assert "containers stopped" in parsed["message"]
    assert parsed["details"]["home"] == str(tmp_path)


def test_teardown_json_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--json`` 失败 → ``{ok: false, message}`` + exit 1。"""
    import json as _json

    def fake_compose_down(*a: Any, **kw: Any) -> None:
        raise DockerError("docker daemon down")

    monkeypatch.setattr(td_mod, "compose_down", fake_compose_down)

    result = _run(_build_app(), "--home", str(tmp_path), "--json")

    assert result.exit_code == 1
    parsed = _json.loads(result.output)
    assert parsed["ok"] is False
    assert "teardown failed" in parsed["message"]
    assert "docker daemon down" in parsed["message"]
