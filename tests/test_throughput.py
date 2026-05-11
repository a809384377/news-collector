"""``newsbox.utils.throughput`` 单元测试（s9 Step 3）。

覆盖：
- ``count_articles_raw`` SQL 过滤行为：domain / since / source_types 组合
- ``gate_read_volume`` 四态决策：below / tty confirm / --json / --yes / 非 tty
"""
from __future__ import annotations

from datetime import timedelta

import pytest
import typer

from newsbox.utils import throughput
from newsbox.utils.throughput import (
    count_articles_raw,
    gate_read_volume,
)
from tests.conftest import ANCHOR


# ---- count_articles_raw ----------------------------------------------------


def test_count_default_domain_excludes_pure_finance(populated_raw_db) -> None:
    """默认 domain='ai'：19 行 ai-domain（含 dotey-14d 多 domain），不含单 finance。"""
    db_path, _conn = populated_raw_db
    n = count_articles_raw(db_path)
    assert n == 19


def test_count_domain_finance(populated_raw_db) -> None:
    """domain='finance'：fin-2d（纯 finance）+ dotey-14d（多 domain）= 2。"""
    db_path, _conn = populated_raw_db
    n = count_articles_raw(db_path, domain="finance")
    assert n == 2


def test_count_since_filter(populated_raw_db) -> None:
    """since=24h（按 ANCHOR）：cl-1h / an-1h / dotey-12h / an-23h = 4 行 ai。"""
    db_path, _conn = populated_raw_db
    since = ANCHOR - timedelta(hours=24)
    n = count_articles_raw(db_path, since=since)
    assert n == 4


def test_count_source_types_rss_only(populated_raw_db) -> None:
    """source_types=['rss']：排除 web 行（cl-old-1 / cl-14d / cl-3d / cl-1h 4 行 web）。"""
    db_path, _conn = populated_raw_db
    n = count_articles_raw(db_path, source_types=["rss"])
    # 19 ai 行 - 4 web ai 行 = 15
    assert n == 15


# ---- gate_read_volume ------------------------------------------------------


def test_gate_below_threshold_noop(capsys: pytest.CaptureFixture[str]) -> None:
    """predicted <= threshold → 静默直返，stderr 无输出。"""
    gate_read_volume(100, 1000, as_json=False, yes=False)
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


def test_gate_above_with_json_does_not_abort(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """超阈值 + as_json=True → 仅 warn，不抛 Exit。"""
    gate_read_volume(99999, 10000, as_json=True, yes=False)
    captured = capsys.readouterr()
    assert "[warn]" in captured.err
    assert "99,999" in captured.err


def test_gate_above_with_yes_does_not_abort(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """超阈值 + yes=True → 仅 warn，不抛 Exit。"""
    gate_read_volume(50000, 10000, as_json=False, yes=True)
    captured = capsys.readouterr()
    assert "[warn]" in captured.err


def test_gate_above_non_tty_no_yes_no_json_aborts(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """超阈值 + 非 tty + 无 --yes + 无 --json → Exit(1) + 引导文案。"""
    monkeypatch.setattr(throughput, "_stdin_is_tty", lambda: False)
    with pytest.raises(typer.Exit) as exc:
        gate_read_volume(99999, 10000, as_json=False, yes=False)
    assert exc.value.exit_code == 1
    captured = capsys.readouterr()
    assert "[warn]" in captured.err
    assert "非交互环境" in captured.err


def test_gate_above_tty_confirm_no_aborts(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """超阈值 + tty + 用户答 no → Exit(1)。"""
    monkeypatch.setattr(throughput, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(typer, "confirm", lambda *a, **kw: False)
    with pytest.raises(typer.Exit) as exc:
        gate_read_volume(99999, 10000, as_json=False, yes=False)
    assert exc.value.exit_code == 1
    captured = capsys.readouterr()
    assert "[warn]" in captured.err


def test_gate_above_tty_confirm_yes_continues(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """超阈值 + tty + 用户答 yes → 不抛 Exit。"""
    monkeypatch.setattr(throughput, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(typer, "confirm", lambda *a, **kw: True)
    gate_read_volume(99999, 10000, as_json=False, yes=False)
    captured = capsys.readouterr()
    assert "[warn]" in captured.err
