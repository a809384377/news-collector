"""``news-collector state`` 命令测试。

覆盖：
1. raw.db 不存在 → exit 1 + 错误提示
2. source_state 空表 → exit 0 + 空提示
3. 行按 last_fetch_at DESC 排序（NULL 在末尾）
4. ``--source-type`` 过滤
5. ``--limit`` 截断
6. 末尾汇总行的 failure 计数
7. 长 last_error 截断到 60 字符
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from typer.testing import CliRunner

from news_collector.commands.state import state_cmd
from news_collector.db import get_conn, init_db


# ---- helpers ---------------------------------------------------------------


def _make_app() -> typer.Typer:
    """单命令 typer app。

    注意：当 typer.Typer 只挂一个命令时，typer 会自动「扁平化」，把这个命令当成
    根命令来调用，CLI 上不需要再写命令名 ``state``。所以测试里 ``runner.invoke``
    的参数列表不包含 ``state``。
    """
    app = typer.Typer()
    app.command("state")(state_cmd)
    return app


def _seed_state(db_path: Path, rows: list[dict[str, Any]]) -> None:
    """新建 raw.db 并插入 source_state 行。"""
    init_db(db_path)
    conn = get_conn(db_path)
    try:
        conn.executemany(
            "INSERT INTO source_state "
            "(source_type, source_id, last_fetch_at, last_error, consecutive_failures) "
            "VALUES (:source_type, :source_id, :last_fetch_at, :last_error, :consecutive_failures)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _run(app: typer.Typer, *args: str) -> Any:
    runner = CliRunner()
    # 单命令 typer app 已扁平化，无需在 args 前再加 "state"。
    return runner.invoke(app, list(args))


# ---- tests -----------------------------------------------------------------


def test_state_db_not_exists(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    # 不创建 raw.db

    result = _run(_make_app(), "--home", str(home))

    assert result.exit_code == 1
    # CliRunner 默认把 stderr 合到 output；用 output 兼容
    assert "raw.db 未找到" in result.output


def test_state_empty_table(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    db_path = home / "raw.db"
    init_db(db_path)  # 建库但不插任何 source_state 行

    result = _run(_make_app(), "--home", str(home))

    assert result.exit_code == 0
    assert "no source state recorded" in result.output


def test_state_displays_rows_sorted_by_last_fetch_desc(tmp_path: Path) -> None:
    """三行：两个有时间戳的 + 一个 NULL；NULL 排末尾，时间戳大的在前。"""
    home = tmp_path / "home"
    home.mkdir()
    db_path = home / "raw.db"
    _seed_state(
        db_path,
        [
            {
                "source_type": "rss",
                "source_id": "older_one",
                "last_fetch_at": "2026-05-09T10:00:00",
                "last_error": None,
                "consecutive_failures": 0,
            },
            {
                "source_type": "rss",
                "source_id": "never_fetched",
                "last_fetch_at": None,
                "last_error": None,
                "consecutive_failures": 0,
            },
            {
                "source_type": "rss",
                "source_id": "newest_one",
                "last_fetch_at": "2026-05-09T23:45:12",
                "last_error": None,
                "consecutive_failures": 0,
            },
        ],
    )

    result = _run(_make_app(), "--home", str(home))

    assert result.exit_code == 0
    out = result.output
    # 三行都出现
    assert "newest_one" in out
    assert "older_one" in out
    assert "never_fetched" in out

    # 顺序：newest_one < older_one < never_fetched
    pos_newest = out.index("newest_one")
    pos_older = out.index("older_one")
    pos_never = out.index("never_fetched")
    assert pos_newest < pos_older < pos_never

    # NULL 行显示 "never"
    assert "never" in out

    # 汇总行：3 sources, 0 with failures
    assert "(3 sources, 0 with failures)" in out


def test_state_filters_by_source_type(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    db_path = home / "raw.db"
    _seed_state(
        db_path,
        [
            {
                "source_type": "rss",
                "source_id": "rss_a",
                "last_fetch_at": "2026-05-09T10:00:00",
                "last_error": None,
                "consecutive_failures": 0,
            },
            {
                "source_type": "rss",
                "source_id": "rss_b",
                "last_fetch_at": "2026-05-09T10:05:00",
                "last_error": None,
                "consecutive_failures": 0,
            },
            {
                "source_type": "web",
                "source_id": "web_a",
                "last_fetch_at": "2026-05-09T10:10:00",
                "last_error": None,
                "consecutive_failures": 0,
            },
            {
                "source_type": "web",
                "source_id": "web_b",
                "last_fetch_at": "2026-05-09T10:15:00",
                "last_error": None,
                "consecutive_failures": 0,
            },
        ],
    )

    result = _run(_make_app(), "--home", str(home), "--source-type", "rss")

    assert result.exit_code == 0
    out = result.output
    assert "rss_a" in out
    assert "rss_b" in out
    assert "web_a" not in out
    assert "web_b" not in out
    assert "(2 sources, 0 with failures)" in out


def test_state_limit_caps_rows(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    db_path = home / "raw.db"
    rows = [
        {
            "source_type": "rss",
            "source_id": f"src_{i}",
            "last_fetch_at": f"2026-05-09T10:0{i}:00",
            "last_error": None,
            "consecutive_failures": 0,
        }
        for i in range(5)
    ]
    _seed_state(db_path, rows)

    result = _run(_make_app(), "--home", str(home), "--limit", "2")

    assert result.exit_code == 0
    out = result.output
    # 应看到最近 2 条（src_4 + src_3），其它不出现
    assert "src_4" in out
    assert "src_3" in out
    assert "src_2" not in out
    assert "src_1" not in out
    assert "src_0" not in out
    # 汇总行展示的是「实际显示的行数」
    assert "(2 sources, 0 with failures)" in out


def test_state_failures_summary_line(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    db_path = home / "raw.db"
    _seed_state(
        db_path,
        [
            {
                "source_type": "rss",
                "source_id": "ok_src",
                "last_fetch_at": "2026-05-09T10:00:00",
                "last_error": None,
                "consecutive_failures": 0,
            },
            {
                "source_type": "web",
                "source_id": "bad_src",
                "last_fetch_at": "2026-05-09T10:01:00",
                "last_error": "httpx.ReadTimeout",
                "consecutive_failures": 3,
            },
        ],
    )

    result = _run(_make_app(), "--home", str(home))

    assert result.exit_code == 0
    out = result.output
    assert "ok_src" in out
    assert "bad_src" in out
    assert "httpx.ReadTimeout" in out
    assert "(2 sources, 1 with failures)" in out


def test_state_long_error_truncated(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    db_path = home / "raw.db"
    long_err = "X" * 100
    _seed_state(
        db_path,
        [
            {
                "source_type": "rss",
                "source_id": "noisy",
                "last_fetch_at": "2026-05-09T10:00:00",
                "last_error": long_err,
                "consecutive_failures": 1,
            },
        ],
    )

    result = _run(_make_app(), "--home", str(home))

    assert result.exit_code == 0
    out = result.output
    # 完整 100 字符串不应出现
    assert long_err not in out
    # 但截断后的 59 'X' + '…' 应出现
    truncated = "X" * 59 + "…"
    assert truncated in out
