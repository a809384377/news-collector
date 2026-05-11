"""``newsbox status`` 命令测试。

覆盖：
1. 全绿：容器 Up + 数据库有行 + 失败 0 → exit 0 + 三段都展示
2. docker 不可用：DockerError → "(docker unavailable: ...)" + 不影响其他段
3. raw.db 不存在 → "(raw.db not found ...)" + Containers 段仍正常
4. source_state 全无失败 → "Recent failures" 段显示 "(no failures recorded)"
5. 失败 7 条 → 只展示 top 5 + 按 consecutive_failures DESC 排序
6. last_error 长度 100 → 截断到 60（59 字符 + '…'）

注意 monkeypatch 落点：``status_module.container_status``，因为 status.py 顶部
``from .docker_helpers import container_status, DockerError`` 已把符号 import 到
status_module 命名空间，patch 源头 ``docker_helpers.container_status`` 不影响
已绑定的 alias。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

import newsbox.commands.status as status_module
from newsbox.commands.status import status_cmd
from newsbox.db import get_conn, init_db


# ---- helpers ---------------------------------------------------------------


def _make_app() -> typer.Typer:
    """单命令 typer app；调用时不需要再加命令名。"""
    app = typer.Typer()
    app.command("status")(status_cmd)
    return app


def _run(app: typer.Typer, *args: str) -> Any:
    runner = CliRunner()
    # 单命令 typer app 已扁平化，调用时不需要再加命令名 ``status``。
    return runner.invoke(app, list(args))


def _mock_container_status(
    monkeypatch: pytest.MonkeyPatch,
    ret_or_exc: Any,
) -> None:
    """ret_or_exc：dict 直接返回；Exception 实例则抛。"""

    def fake(*_: Any, **__: Any) -> Any:
        if isinstance(ret_or_exc, Exception):
            raise ret_or_exc
        return ret_or_exc

    monkeypatch.setattr(status_module, "container_status", fake)


def _seed_db(
    db_path: Path,
    *,
    articles_count: int = 0,
    state_rows: list[dict[str, Any]] | None = None,
) -> None:
    """新建 raw.db；可选插入 articles_raw 假行 + source_state 行。"""
    init_db(db_path)
    conn = get_conn(db_path)
    try:
        if state_rows:
            conn.executemany(
                "INSERT INTO source_state "
                "(source_type, source_id, last_fetch_at, last_error, consecutive_failures) "
                "VALUES (:source_type, :source_id, :last_fetch_at, :last_error, :consecutive_failures)",
                state_rows,
            )
        for i in range(articles_count):
            conn.execute(
                "INSERT INTO articles_raw "
                "(source_type, source_id, source_tier, external_id, "
                " url, url_canonical_hash, content_hash, title, body, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "rss",
                    "test",
                    "kol",
                    f"ext-{i}",
                    f"https://t.example/{i}",
                    f"hash-{i}",
                    f"chash-{i}",
                    f"title {i}",
                    "body",
                    "2026-05-09T00:00:00+00:00",
                ),
            )
        conn.commit()
    finally:
        conn.close()


# ---- tests -----------------------------------------------------------------


def test_status_all_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    db_path = home / "raw.db"
    _seed_db(
        db_path,
        articles_count=3,
        state_rows=[
            {
                "source_type": "rss",
                "source_id": "src_a",
                "last_fetch_at": "2026-05-09T23:45:12",
                "last_error": None,
                "consecutive_failures": 0,
            },
        ],
    )
    _mock_container_status(monkeypatch, {"rsshub": "Up", "redis": "Up"})

    result = _run(_make_app(), "--home", str(home))

    assert result.exit_code == 0
    out = result.output
    # 三段标题都在
    assert "== Containers ==" in out
    assert "== Database ==" in out
    assert "== Recent failures (top 5) ==" in out
    # 容器都 Up
    assert "rsshub" in out
    assert "redis" in out
    assert "Up" in out
    # 数据库行数 + 时间 + 路径
    assert "total rows  : 3" in out
    assert "2026-05-09 23:45:12" in out
    assert str(db_path) in out
    # 没有失败
    assert "(no failures recorded)" in out


def test_status_docker_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    db_path = home / "raw.db"
    _seed_db(db_path, articles_count=1)
    _mock_container_status(
        monkeypatch, status_module.DockerError("docker daemon 未运行")
    )

    result = _run(_make_app(), "--home", str(home))

    assert result.exit_code == 0
    out = result.output
    # v0.5.3 改为多行展示（标题 + 内容缩进），不再用括号包装
    assert "docker unavailable:" in out
    assert "docker daemon 未运行" in out
    # 其他段仍输出
    assert "== Database ==" in out
    assert "total rows  : 1" in out
    assert "== Recent failures (top 5) ==" in out


def test_status_db_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    # 不建 raw.db
    _mock_container_status(monkeypatch, {"rsshub": "Up", "redis": "Up"})

    result = _run(_make_app(), "--home", str(home))

    assert result.exit_code == 0
    out = result.output
    # Containers 段正常
    assert "rsshub" in out
    assert "Up" in out
    # Database 段降级
    assert "(raw.db not found" in out
    # Recent failures 段也降级（不重复 sqlite 错误）
    assert "(no failures recorded)" in out


def test_status_no_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    db_path = home / "raw.db"
    _seed_db(
        db_path,
        articles_count=0,
        state_rows=[
            {
                "source_type": "rss",
                "source_id": "ok_a",
                "last_fetch_at": "2026-05-09T10:00:00",
                "last_error": None,
                "consecutive_failures": 0,
            },
            {
                "source_type": "web",
                "source_id": "ok_b",
                "last_fetch_at": "2026-05-09T11:00:00",
                "last_error": None,
                "consecutive_failures": 0,
            },
        ],
    )
    _mock_container_status(monkeypatch, {"rsshub": "Up", "redis": "Up"})

    result = _run(_make_app(), "--home", str(home))

    assert result.exit_code == 0
    out = result.output
    assert "(no failures recorded)" in out
    # 没有失败信源时不应混入失败行（ok_a / ok_b 不应出现在 failures 段）
    # 简单校验：ok_a 不应作为失败行展示（即 ok_a 不出现在输出里，因为只有 failures 段会列信源 id）
    assert "ok_a" not in out
    assert "ok_b" not in out


def test_status_failures_top5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    db_path = home / "raw.db"
    # 7 条 failure，consecutive_failures 1..7
    state_rows = [
        {
            "source_type": "rss",
            "source_id": f"bad_{i}",
            "last_fetch_at": "2026-05-09T10:00:00",
            "last_error": f"err {i}",
            "consecutive_failures": i,
        }
        for i in range(1, 8)
    ]
    _seed_db(db_path, state_rows=state_rows)
    _mock_container_status(monkeypatch, {"rsshub": "Up", "redis": "Up"})

    result = _run(_make_app(), "--home", str(home))

    assert result.exit_code == 0
    out = result.output
    # top 5 应是 7,6,5,4,3
    for i in (3, 4, 5, 6, 7):
        assert f"bad_{i}" in out
    # bad_1 / bad_2 不应出现
    assert "bad_1" not in out
    assert "bad_2" not in out

    # 验证降序：bad_7 在 bad_6 前面，bad_3 在最后
    pos7 = out.index("bad_7")
    pos6 = out.index("bad_6")
    pos5 = out.index("bad_5")
    pos4 = out.index("bad_4")
    pos3 = out.index("bad_3")
    assert pos7 < pos6 < pos5 < pos4 < pos3


def test_status_long_error_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    db_path = home / "raw.db"
    long_err = "X" * 100
    _seed_db(
        db_path,
        state_rows=[
            {
                "source_type": "rss",
                "source_id": "noisy",
                "last_fetch_at": "2026-05-09T10:00:00",
                "last_error": long_err,
                "consecutive_failures": 1,
            },
        ],
    )
    _mock_container_status(monkeypatch, {"rsshub": "Up", "redis": "Up"})

    result = _run(_make_app(), "--home", str(home))

    assert result.exit_code == 0
    out = result.output
    assert long_err not in out
    truncated = "X" * 59 + "…"
    assert truncated in out


# ---- --json tests (s9 Step 2) ---------------------------------------------


def test_status_json_all_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--json 全绿：containers.available + database 有行 + recent_failures 空。"""
    import json as _json

    home = tmp_path / "home"
    home.mkdir()
    db_path = home / "raw.db"
    _seed_db(
        db_path,
        articles_count=3,
        state_rows=[
            {
                "source_type": "rss",
                "source_id": "src_a",
                "last_fetch_at": "2026-05-09T23:45:12",
                "last_error": None,
                "consecutive_failures": 0,
            },
        ],
    )
    _mock_container_status(monkeypatch, {"rsshub": "Up", "redis": "Up"})

    result = _run(_make_app(), "--home", str(home), "--json")

    assert result.exit_code == 0
    payload = _json.loads(result.output)
    # 顶层 schema
    assert payload["home"] == str(home)
    assert set(payload.keys()) == {
        "home",
        "containers",
        "database",
        "recent_failures",
    }
    # containers 段
    assert payload["containers"]["available"] is True
    assert payload["containers"]["services"] == {"rsshub": "Up", "redis": "Up"}
    # database 段
    assert payload["database"]["exists"] is True
    assert payload["database"]["total_rows"] == 3
    assert payload["database"]["path"] == str(db_path)
    assert payload["database"]["last_fetch_at"] == "2026-05-09T23:45:12"
    # recent_failures 段
    assert payload["recent_failures"]["top_n"] == 5
    assert payload["recent_failures"]["items"] == []


def test_status_json_docker_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--json 模式下 docker 错误以 containers.available=false 表达，不破坏 JSON。"""
    import json as _json

    home = tmp_path / "home"
    home.mkdir()
    db_path = home / "raw.db"
    _seed_db(db_path, articles_count=1)
    _mock_container_status(
        monkeypatch, status_module.DockerError("docker daemon 未运行")
    )

    result = _run(_make_app(), "--home", str(home), "--json")

    assert result.exit_code == 0
    payload = _json.loads(result.output)
    assert payload["containers"]["available"] is False
    assert "docker daemon 未运行" in payload["containers"]["error"]
    # 数据库段仍正常
    assert payload["database"]["exists"] is True
    assert payload["database"]["total_rows"] == 1


def test_status_json_db_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--json + raw.db 缺失：database.exists=false，仍 exit 0。"""
    import json as _json

    home = tmp_path / "home"
    home.mkdir()
    _mock_container_status(monkeypatch, {"rsshub": "Up", "redis": "Up"})

    result = _run(_make_app(), "--home", str(home), "--json")

    assert result.exit_code == 0
    payload = _json.loads(result.output)
    assert payload["database"]["exists"] is False
    # 缺失时不应有 total_rows / size_bytes 字段
    assert "total_rows" not in payload["database"]
    assert payload["recent_failures"]["items"] == []


def test_status_json_failures_top5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--json failures 段返回 top 5 + 按 consecutive_failures DESC 排序。"""
    import json as _json

    home = tmp_path / "home"
    home.mkdir()
    db_path = home / "raw.db"
    state_rows = [
        {
            "source_type": "rss",
            "source_id": f"bad_{i}",
            "last_fetch_at": "2026-05-09T10:00:00",
            "last_error": f"err {i}",
            "consecutive_failures": i,
        }
        for i in range(1, 8)
    ]
    _seed_db(db_path, state_rows=state_rows)
    _mock_container_status(monkeypatch, {"rsshub": "Up", "redis": "Up"})

    result = _run(_make_app(), "--home", str(home), "--json")

    assert result.exit_code == 0
    payload = _json.loads(result.output)
    items = payload["recent_failures"]["items"]
    assert len(items) == 5
    # 降序：7, 6, 5, 4, 3
    assert [r["consecutive_failures"] for r in items] == [7, 6, 5, 4, 3]
    assert items[0]["source_id"] == "bad_7"
    # JSON 段保留原始 last_error（不截断）
    assert items[0]["last_error"] == "err 7"
