"""``commands._json`` helper 单测（s9 Step 2 引入）。

覆盖：
1. ``emit(payload)`` 序列化任意 payload 到 stdout
2. ``emit_ok`` 无参 → ``{"ok": true}``
3. ``emit_ok`` 带 message + details → 三字段都出现
4. ``emit_err`` → ``{"ok": false, "message": ...}``，details 透传
5. JSON 输出能被 ``json.loads`` 解析（不抛错）
6. ``json_option`` 注册的命令支持 ``--json`` flag

注意：测试通过 capsys 捕获 stdout/stderr；emit_* helper 走 typer.echo
（默认 stdout），所以从 ``capsys.readouterr().out`` 取。
"""
from __future__ import annotations

import json
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from newsbox.commands._json import emit, emit_err, emit_ndjson, emit_ok, json_option


# ---- emit -----------------------------------------------------------------


def test_emit_dict(capsys: pytest.CaptureFixture[str]) -> None:
    emit({"foo": "bar", "n": 42})
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed == {"foo": "bar", "n": 42}


def test_emit_list(capsys: pytest.CaptureFixture[str]) -> None:
    emit([1, 2, 3])
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed == [1, 2, 3]


def test_emit_handles_non_serializable_via_default_str(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from pathlib import Path

    emit({"home": Path("/tmp/test")})
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed == {"home": "/tmp/test"}


def test_emit_ndjson_one_line_per_item(
    capsys: pytest.CaptureFixture[str],
) -> None:
    emit_ndjson([{"i": 1}, {"i": 2}, {"i": 3}])
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 3
    assert json.loads(lines[0]) == {"i": 1}
    assert json.loads(lines[2]) == {"i": 3}


def test_emit_ndjson_empty(capsys: pytest.CaptureFixture[str]) -> None:
    emit_ndjson([])
    assert capsys.readouterr().out == ""


# ---- emit_ok --------------------------------------------------------------


def test_emit_ok_minimal(capsys: pytest.CaptureFixture[str]) -> None:
    emit_ok()
    parsed = json.loads(capsys.readouterr().out)
    assert parsed == {"ok": True}


def test_emit_ok_with_message(capsys: pytest.CaptureFixture[str]) -> None:
    emit_ok("done")
    parsed = json.loads(capsys.readouterr().out)
    assert parsed == {"ok": True, "message": "done"}


def test_emit_ok_with_details(capsys: pytest.CaptureFixture[str]) -> None:
    emit_ok("done", home="/tmp/x", count=3)
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["ok"] is True
    assert parsed["message"] == "done"
    assert parsed["details"] == {"home": "/tmp/x", "count": 3}


# ---- emit_err -------------------------------------------------------------


def test_emit_err_minimal(capsys: pytest.CaptureFixture[str]) -> None:
    emit_err("something failed")
    parsed = json.loads(capsys.readouterr().out)
    assert parsed == {"ok": False, "message": "something failed"}


def test_emit_err_with_details(capsys: pytest.CaptureFixture[str]) -> None:
    emit_err("file not found", path="/tmp/x")
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["ok"] is False
    assert parsed["message"] == "file not found"
    assert parsed["details"] == {"path": "/tmp/x"}


def test_emit_err_does_not_raise(capsys: pytest.CaptureFixture[str]) -> None:
    """emit_err 不抛 typer.Exit；调用方决定 exit code。"""
    emit_err("bad")  # 不应抛
    assert "bad" in capsys.readouterr().out


# ---- json_option ----------------------------------------------------------


def test_json_option_registers_flag() -> None:
    """挂载 json_option 的命令应识别 ``--json`` 字面量。"""

    def cmd(json_output: bool = json_option()) -> None:
        if json_output:
            emit({"hello": "world"})
        else:
            typer.echo("plain")

    app = typer.Typer()
    app.command("show")(cmd)
    app.command("_placeholder", hidden=True)(lambda: None)
    runner = CliRunner()

    plain = runner.invoke(app, ["show"])
    assert plain.exit_code == 0
    assert "plain" in plain.output

    js = runner.invoke(app, ["show", "--json"])
    assert js.exit_code == 0
    parsed = json.loads(js.output)
    assert parsed == {"hello": "world"}
