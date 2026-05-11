"""``news-collector clean`` 命令测试。

s5-data-views D5 修正：clean 不再做 stdin tty 检测。dry-run 是无害操作，所有
环境（tty / 非 tty / piped stdin）都直接走 dry-run；真删一律由 ``--yes`` 显
式确认。原非 tty 防呆路径已删除（R-3 适用于 typer.prompt 调用，clean 没有
prompt 交互，--yes 标志本身已是显式确认）。

覆盖：

1. 空库 dry-run → "Articles to delete: 0"
2. 空库 ``--yes`` 不报错且 db 仍存在
3. raw.db 不存在 → exit 1
4. ``--before`` 缺失 → exit 2（typer 必填校验）
5. ``--before=invalid`` → exit 2 + stderr 报错
6. populated_raw_db + ``--before=2026-04-10`` dry-run → 4 条待删
7. populated_raw_db + ``--before=2026-04-10 --yes`` → 真删 20→16
8. 真删后再 dry-run → 0 条待删
9. ``--no-vacuum --yes`` → 不输出 "Running VACUUM"
10. dry-run 输出包含 DRY RUN / Articles to delete / keep 字样
11. ``--yes`` 输出包含 Deleting / done / Running VACUUM 字样

注意：
- 测试函数前缀避开 ``setup_/teardown_``（pytest xunit hook 名）—— 测试模块里
  这两个前缀的函数会被当成 hook，导致 AttributeError（s3-cli-onboarding L13）。
- 单命令 typer app 会被 typer 自动扁平化；要么 invoke 时不前置命令名，要么
  挂一个 hidden _placeholder 命令强制 group 模式。这里采用后者风格，与
  test_setup_cmd.py 保持一致。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import typer
from typer.testing import CliRunner

from news_collector.commands import clean as clean_mod


# ---- helpers ---------------------------------------------------------------


def _build_app() -> typer.Typer:
    """挂 clean_cmd + 一个 hidden 占位命令，防 typer 单命令扁平化。"""
    app = typer.Typer()
    app.command("clean")(clean_mod.clean_cmd)
    app.command("_placeholder", hidden=True)(lambda: None)
    return app


def _run(app: typer.Typer, *args: str) -> Any:
    runner = CliRunner()
    return runner.invoke(app, ["clean", *args])


def _count_articles(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return int(conn.execute("SELECT COUNT(*) FROM articles_raw").fetchone()[0])
    finally:
        conn.close()


# ---- tests -----------------------------------------------------------------


def test_clean_db_not_exists(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    # 不创建 raw.db

    result = _run(_build_app(), "--home", str(home), "--before", "30d")

    assert result.exit_code == 1
    assert "raw.db 未找到" in result.output


def test_clean_before_missing_exits_2(tmp_raw_db) -> None:
    db_path, _conn = tmp_raw_db
    home = db_path.parent

    result = _run(_build_app(), "--home", str(home))
    # typer 必填校验：缺 --before 应非 0 退出
    assert result.exit_code != 0


def test_clean_before_invalid_exits_2(tmp_raw_db) -> None:
    db_path, _conn = tmp_raw_db
    home = db_path.parent

    result = _run(_build_app(), "--home", str(home), "--before", "not-a-time")
    assert result.exit_code == 2
    assert "解析失败" in result.output


def test_clean_empty_db_dry_run(tmp_raw_db) -> None:
    db_path, _conn = tmp_raw_db
    home = db_path.parent

    result = _run(_build_app(), "--home", str(home), "--before", "30d")

    assert result.exit_code == 0
    out = result.output
    assert "DRY RUN" in out
    assert "Articles to delete:" in out
    # 0 条要删（width=6 右对齐）
    assert "Articles to delete:         0" in out
    assert "Articles to keep:" in out
    assert "Total in raw.db:" in out


def test_clean_empty_db_yes_runs_clean(tmp_raw_db) -> None:
    db_path, conn = tmp_raw_db
    conn.close()  # clean_cmd 会自己开连接，避免锁竞争
    home = db_path.parent

    result = _run(_build_app(), "--home", str(home), "--before", "30d", "--yes")

    assert result.exit_code == 0
    assert db_path.exists()
    assert "Deleting 0 articles" in result.output
    # 默认带 vacuum
    assert "Running VACUUM" in result.output


def test_clean_populated_dry_run_30d(populated_raw_db) -> None:
    """fixture 数据 30d 前有 4 行（an-old-1/2 60d/58d、cl-old-1 55d、simon-31d）。"""
    db_path, conn = populated_raw_db
    conn.close()
    home = db_path.parent

    # 用绝对日期 2026-04-10 = ANCHOR(2026-05-10) - 30d
    result = _run(_build_app(), "--home", str(home), "--before", "2026-04-10")

    assert result.exit_code == 0
    out = result.output
    assert "DRY RUN" in out
    # 待删 4 / 保留 16 / 总共 20（width=6 右对齐）
    assert "Articles to delete:         4" in out
    assert "Articles to keep:          16" in out
    assert "Total in raw.db:           20" in out
    # 数据库未被改动
    assert _count_articles(db_path) == 20


def test_clean_populated_yes_actually_deletes(populated_raw_db) -> None:
    db_path, conn = populated_raw_db
    conn.close()
    home = db_path.parent

    result = _run(
        _build_app(),
        "--home",
        str(home),
        "--before",
        "2026-04-10",
        "--yes",
    )

    assert result.exit_code == 0
    out = result.output
    assert "Deleting 4 articles" in out
    assert "done" in out
    assert "Running VACUUM" in out
    # 真的删了
    assert _count_articles(db_path) == 16


def test_clean_yes_then_dry_run_shows_zero(populated_raw_db) -> None:
    db_path, conn = populated_raw_db
    conn.close()
    home = db_path.parent

    # 第一次 --yes 删 4 行
    r1 = _run(
        _build_app(),
        "--home",
        str(home),
        "--before",
        "2026-04-10",
        "--yes",
    )
    assert r1.exit_code == 0

    # 第二次 dry-run，相同 cutoff 应 0 条要删
    r2 = _run(_build_app(), "--home", str(home), "--before", "2026-04-10")
    assert r2.exit_code == 0
    assert "Articles to delete:         0" in r2.output


def test_clean_no_vacuum_skips_vacuum(populated_raw_db) -> None:
    db_path, conn = populated_raw_db
    conn.close()
    home = db_path.parent

    result = _run(
        _build_app(),
        "--home",
        str(home),
        "--before",
        "2026-04-10",
        "--yes",
        "--no-vacuum",
    )

    assert result.exit_code == 0
    out = result.output
    assert "Deleting 4 articles" in out
    assert "Running VACUUM" not in out
    # 也不应有末尾 ⚠ hint
    assert "VACUUM 期间数据库加锁" not in out
    # 数据真删了
    assert _count_articles(db_path) == 16


def test_clean_dry_run_runs_in_any_environment(populated_raw_db) -> None:
    """D5：dry-run 是无害操作，所有环境都能跑（不再做 stdin tty 检测）。
    pytest 跑时 stdin 本就非 tty，能跑通即证明 dry-run 不被拦截。
    """
    db_path, conn = populated_raw_db
    conn.close()
    home = db_path.parent

    result = _run(_build_app(), "--home", str(home), "--before", "2026-04-10")

    assert result.exit_code == 0
    assert "DRY RUN" in result.output
    # 不删
    assert _count_articles(db_path) == 20


def test_clean_dry_run_output_format(populated_raw_db) -> None:
    """dry-run 输出包含约定字样。"""
    db_path, conn = populated_raw_db
    conn.close()
    home = db_path.parent

    result = _run(_build_app(), "--home", str(home), "--before", "2026-04-10")

    assert result.exit_code == 0
    out = result.output
    assert "[dry-run] news-collector clean" in out
    assert "DRY RUN" in out
    assert "Articles to delete:" in out
    assert "Articles to keep:" in out
    assert "Total in raw.db:" in out
    assert "cutoff (fetched_at <):" in out
    assert "(--before=2026-04-10)" in out


def test_clean_yes_output_format(populated_raw_db) -> None:
    """--yes 输出包含约定字样。"""
    db_path, conn = populated_raw_db
    conn.close()
    home = db_path.parent

    result = _run(
        _build_app(),
        "--home",
        str(home),
        "--before",
        "2026-04-10",
        "--yes",
    )

    assert result.exit_code == 0
    out = result.output
    assert "[execute] news-collector clean --yes" in out
    assert "Deleting" in out
    assert "done" in out
    assert "Running VACUUM" in out
    assert "MB" in out  # size 报告里有 "MB" 单位
    assert "VACUUM 期间数据库加锁" in out
