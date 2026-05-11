"""``newsbox logs`` 命令测试。

覆盖点：
1. 日志文件不存在 → exit 1 + 错误提示
2. 日志文件 0 字节 → exit 0 + "log file is empty" 提示
3. 默认 ``--tail=50``：写入 100 行只显示最后 50 行
4. 自定义 ``--tail=N``：写入 100 行 ``--tail=10`` 显示最后 10 行
5. 含非法 utf-8 字节（如 0xff）：errors='replace' 兜底不崩溃

测试 fixture 模式：
- 用 ``tmp_path / "home"`` 作为运行时目录
- 写 ``config.yaml`` 把 ``logging.file`` 改为相对路径，使日志落到 home/logs/
- ``logging_setup.reset_for_tests()`` 反复重置模块级状态，避免 sink 互扰
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer
import yaml
from typer.testing import CliRunner

from newsbox import logging_setup
from newsbox.commands.logs import logs_cmd


@pytest.fixture(autouse=True)
def _reset_logging_setup_state() -> None:
    """各用例前后重置 logging_setup 模块级状态，避免 file sink 残留互扰。"""
    logging_setup.reset_for_tests()
    yield
    logging_setup.reset_for_tests()


def _build_app() -> typer.Typer:
    """构造一个挂了 logs 命令的临时 typer app，便于 CliRunner 调用。

    注：典型 Typer 在只有 1 个 command 时会自动扁平化为根；为了保持「子命令」
    调用形式（``runner.invoke(app, ["logs", ...])``），多注册一个占位命令强制
    group 模式。
    """
    app = typer.Typer()
    app.command("logs")(logs_cmd)

    @app.command("_placeholder", hidden=True)
    def _placeholder() -> None:  # pragma: no cover - 仅用于强制 typer group 模式
        pass

    return app


def _prepare_home(tmp_path: Path) -> Path:
    """准备 home 目录 + 写 config.yaml 把 logging.file 指向 home/logs/...。"""
    home = tmp_path / "home"
    home.mkdir()
    (home / "logs").mkdir()
    config_override = {
        "logging": {
            "level": "info",
            "file": "logs/newsbox.log",  # 相对路径 → 落到 home 下
            "rotation": "daily",
            "retention_days": 30,
        }
    }
    (home / "config.yaml").write_text(
        yaml.safe_dump(config_override), encoding="utf-8"
    )
    return home


def test_logs_file_not_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """日志文件不存在 → exit 1 + 输出含「日志文件不存在」。

    注：``load_app_config`` 内部会调 ``init_logging`` 挂 file sink，副作用是会
    创建空文件。为了真触达「不存在」分支，把 init_logging 替换为 no-op。
    """
    home = _prepare_home(tmp_path)
    log_file = home / "logs" / "newsbox.log"
    if log_file.exists():
        log_file.unlink()

    # 让 init_logging 不真挂 sink（也就不会 touch 出空文件）
    monkeypatch.setattr(
        "newsbox.commands._helpers.logging_setup.init_logging",
        lambda *a, **kw: None,
    )

    runner = CliRunner()
    result = runner.invoke(_build_app(), ["logs", "--home", str(home)])

    assert result.exit_code == 1
    # 错误信息走 stderr（click 9 默认把 stderr 分开到 result.stderr）
    assert "日志文件不存在" in result.stderr
    assert str(log_file) in result.stderr


def test_logs_file_empty(tmp_path: Path) -> None:
    """日志文件存在但 0 字节 → exit 0 + 输出含「log file is empty」。"""
    home = _prepare_home(tmp_path)
    log_file = home / "logs" / "newsbox.log"
    log_file.write_text("", encoding="utf-8")  # 0 字节

    runner = CliRunner()
    result = runner.invoke(_build_app(), ["logs", "--home", str(home)])

    assert result.exit_code == 0
    assert "log file is empty" in result.stdout
    assert str(log_file) in result.stdout


def test_logs_tail_default_50(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写入 100 行，默认 ``--tail=50`` → 显示最后 50 行。"""
    home = _prepare_home(tmp_path)
    log_file = home / "logs" / "newsbox.log"
    lines = [f"line-{i:03d}\n" for i in range(100)]
    log_file.write_text("".join(lines), encoding="utf-8")

    # 让 init_logging 不真挂 sink，避免它在文件末尾追加无关日志干扰断言
    monkeypatch.setattr(
        "newsbox.commands._helpers.logging_setup.init_logging",
        lambda *a, **kw: None,
    )

    runner = CliRunner()
    result = runner.invoke(_build_app(), ["logs", "--home", str(home)])

    assert result.exit_code == 0
    out = result.stdout
    # 最后 50 行 = line-050 ~ line-099
    assert "line-050" in out
    assert "line-099" in out
    # 倒数第 51 行 line-049 不应出现
    assert "line-049" not in out
    # 第一行也不应出现
    assert "line-000" not in out


def test_logs_tail_custom_n(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写入 100 行，``--tail=10`` → 显示最后 10 行。"""
    home = _prepare_home(tmp_path)
    log_file = home / "logs" / "newsbox.log"
    lines = [f"line-{i:03d}\n" for i in range(100)]
    log_file.write_text("".join(lines), encoding="utf-8")

    monkeypatch.setattr(
        "newsbox.commands._helpers.logging_setup.init_logging",
        lambda *a, **kw: None,
    )

    runner = CliRunner()
    result = runner.invoke(
        _build_app(), ["logs", "--home", str(home), "--tail", "10"]
    )

    assert result.exit_code == 0
    out = result.stdout
    # 最后 10 行 = line-090 ~ line-099
    for i in range(90, 100):
        assert f"line-{i:03d}" in out
    # line-089 不应出现
    assert "line-089" not in out
    # 计数：恰好 10 个 "line-" 前缀的匹配
    assert out.count("line-") == 10


def test_logs_json_happy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--json：返回 {path, lines[], tail}，lines 长度等于 tail 上限。"""
    home = _prepare_home(tmp_path)
    log_file = home / "logs" / "newsbox.log"
    lines = [f"line-{i:03d}\n" for i in range(100)]
    log_file.write_text("".join(lines), encoding="utf-8")

    monkeypatch.setattr(
        "newsbox.commands._helpers.logging_setup.init_logging",
        lambda *a, **kw: None,
    )

    runner = CliRunner()
    result = runner.invoke(
        _build_app(),
        ["logs", "--home", str(home), "--tail", "5", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["path"] == str(log_file)
    assert payload["tail"] == 5
    # lines 应为最后 5 行，且去掉末尾换行
    assert payload["lines"] == [
        "line-095",
        "line-096",
        "line-097",
        "line-098",
        "line-099",
    ]


def test_logs_json_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--json + 文件不存在：emit_err + exit 1。"""
    home = _prepare_home(tmp_path)
    log_file = home / "logs" / "newsbox.log"
    if log_file.exists():
        log_file.unlink()

    monkeypatch.setattr(
        "newsbox.commands._helpers.logging_setup.init_logging",
        lambda *a, **kw: None,
    )

    runner = CliRunner()
    result = runner.invoke(
        _build_app(), ["logs", "--home", str(home), "--json"]
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "log file not found" in payload["message"]
    assert payload["details"]["path"] == str(log_file)


def test_logs_handles_invalid_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """日志含非法 utf-8 字节（如 0xff）时 errors='replace' 兜底不崩溃。"""
    home = _prepare_home(tmp_path)
    log_file = home / "logs" / "newsbox.log"
    # 写入二进制：包含 0xff 等非法 utf-8 字节
    log_file.write_bytes(b"good-line-1\nbad-\xff\xfe-line\nfinal-line\n")

    monkeypatch.setattr(
        "newsbox.commands._helpers.logging_setup.init_logging",
        lambda *a, **kw: None,
    )

    runner = CliRunner()
    result = runner.invoke(_build_app(), ["logs", "--home", str(home)])

    assert result.exit_code == 0
    out = result.stdout
    # 三行都应出现（非法字节被 replace 替换为 U+FFFD，但前后文本完好）
    assert "good-line-1" in out
    assert "final-line" in out
    # 含非法字节那行的可见前缀也在
    assert "bad-" in out
