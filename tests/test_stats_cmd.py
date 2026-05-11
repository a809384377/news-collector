"""``news-collector stats`` 命令测试。

覆盖：
1. raw.db 不存在 → exit 1 + 错误提示
2. 空库 → 4 panel 全为 0/空，--json total.articles == 0 + last_7_days 7 行全 0
3. populated_raw_db → total.articles == 20，top_sources 第一位是 anthropic_news
4. --top=3 → 人类视图限 3 行；--json 仍全量
5. last_7_days 注入 ANCHOR：边界日期数据正确，不在 7 天窗的旧数据不计
6. by_source_type_domain：多 domain 行（['ai','finance']）会让 ai 与 finance 两组都各 +1
7. 千位分隔（人类视图）：通过额外灌大数据测 ``1,234`` 渲染
8. earliest / latest fetched_at = 60d 前 / 1h 前那两条
9. --json sources.yaml 存在时 enabled/disabled 数读到
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from news_collector.commands import stats as stats_module
from news_collector.commands.stats import stats_cmd
from news_collector.db import get_conn, init_db
from tests.conftest import ANCHOR


# ---- helpers ---------------------------------------------------------------


def _make_app() -> typer.Typer:
    """单命令 typer app（详见 KNOWLEDGE-LOG #14：单命令会被自动扁平化）。

    通过加一个 hidden ``_placeholder`` 命令避免 typer 把唯一命令当成根命令调用。
    """
    app = typer.Typer()
    app.command("stats")(stats_cmd)
    app.command("_placeholder", hidden=True)(lambda: None)
    return app


def _run(app: typer.Typer, *args: str) -> Any:
    """避免在 test_*.py 模块顶层使用 setup_/teardown_ 别名（KNOWLEDGE-LOG #13）。"""
    runner = CliRunner()
    return runner.invoke(app, ["stats", *args])


def _freeze_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 stats._now_utc 锁到 ANCHOR=2026-05-10 12:00 UTC。

    这样测试结果与系统时钟解耦，CI 在任何日期跑都稳定。
    """
    monkeypatch.setattr(stats_module, "_now_utc", lambda: ANCHOR)


# ---- tests -----------------------------------------------------------------


def test_stats_db_not_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _freeze_now(monkeypatch)

    result = _run(_make_app(), "--home", str(home))

    assert result.exit_code == 1
    assert "raw.db 未找到" in result.output


def test_stats_empty_db_human_view(
    tmp_raw_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, _conn = tmp_raw_db
    home = db_path.parent
    _freeze_now(monkeypatch)

    result = _run(_make_app(), "--home", str(home))

    assert result.exit_code == 0
    out = result.output
    # 标题
    assert "== news-collector stats ==" in out
    # 4 块面板都出现
    assert "[Total]" in out
    assert "[Top sources by article count]" in out
    assert "[Last 7 days new articles]" in out
    assert "[By source_type × domain]" in out
    # 空库占位文案
    assert "articles: 0" in out
    assert "(no sources tracked)" in out
    assert "(no articles in last 7 days)" in out
    assert "(no data)" in out


def test_stats_empty_db_json(
    tmp_raw_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, _conn = tmp_raw_db
    home = db_path.parent
    _freeze_now(monkeypatch)

    result = _run(_make_app(), "--home", str(home), "--json")

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["total"]["articles"] == 0
    assert payload["total"]["enabled_sources"] == 0
    assert payload["total"]["disabled_sources"] == 0
    assert payload["total"]["earliest_fetched_at"] is None
    assert payload["total"]["latest_fetched_at"] is None
    assert payload["top_sources"] == []
    # last_7_days 即使空库也是 7 行 count=0
    assert len(payload["last_7_days"]) == 7
    assert all(row["count"] == 0 for row in payload["last_7_days"])
    assert payload["by_source_type_domain"] == []


def test_stats_populated_total_human(
    populated_raw_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, _conn = populated_raw_db
    home = db_path.parent
    _freeze_now(monkeypatch)

    result = _run(_make_app(), "--home", str(home))

    assert result.exit_code == 0
    out = result.output
    # 总条数
    assert "articles: 20" in out
    # latest 1h 前 = ANCHOR - 1h = 2026-05-10 11:00:00
    assert "2026-05-10 11:00:00" in out
    # earliest = ANCHOR - 60d 整 = 2026-03-11 12:00:00（fixture 里 fetched_at 60d 整）
    assert "2026-03-11 12:00:00" in out


def test_stats_populated_total_json(
    populated_raw_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, _conn = populated_raw_db
    home = db_path.parent
    _freeze_now(monkeypatch)

    result = _run(_make_app(), "--home", str(home), "--json")
    assert result.exit_code == 0
    payload = json.loads(result.output)

    assert payload["total"]["articles"] == 20
    # earliest = ANCHOR - 60d 整（fixture 里 fetched_at 是 60d，published_at 才 -2h）
    expected_earliest = (ANCHOR - timedelta(days=60)).isoformat()
    expected_latest = (ANCHOR - timedelta(hours=1)).isoformat()
    assert payload["total"]["earliest_fetched_at"] == expected_earliest
    assert payload["total"]["latest_fetched_at"] == expected_latest


def test_stats_populated_top_sources_order(
    populated_raw_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """anthropic_news 8 行 → 排第一；finance_demo 1 行 → 排末尾。"""
    db_path, _conn = populated_raw_db
    home = db_path.parent
    _freeze_now(monkeypatch)

    result = _run(_make_app(), "--home", str(home), "--json")
    assert result.exit_code == 0
    payload = json.loads(result.output)

    top = payload["top_sources"]
    # 5 个信源全量返回
    assert len(top) == 5
    assert top[0]["source_id"] == "anthropic_news"
    assert top[0]["count"] == 8
    assert top[0]["rank"] == 1
    # finance_demo 末尾
    last = top[-1]
    assert last["source_id"] == "finance_demo"
    assert last["count"] == 1
    assert last["rank"] == 5

    # 计数加和 = 20
    assert sum(item["count"] for item in top) == 20


def test_stats_top_n_limit_human_only(
    populated_raw_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--top=3 人类视图只显示 3 条 + "showing top 3 of 5 sources"；JSON 全量。"""
    db_path, _conn = populated_raw_db
    home = db_path.parent
    _freeze_now(monkeypatch)

    # 人类视图
    result_human = _run(_make_app(), "--home", str(home), "--top", "3")
    assert result_human.exit_code == 0
    out = result_human.output
    assert "anthropic_news" in out  # rank 1
    assert "showing top 3 of 5 sources" in out
    # 末位 finance_demo 不应出现在 top-3 排行内
    # （finance_demo 字符串只在排行 panel 出现一次，所以可以直接断言不在 out）
    assert "finance_demo" not in out

    # JSON 不受 --top 限制
    result_json = _run(_make_app(), "--home", str(home), "--top", "3", "--json")
    assert result_json.exit_code == 0
    payload = json.loads(result_json.output)
    assert len(payload["top_sources"]) == 5  # 全量


def test_stats_last_7_days_with_anchor(
    populated_raw_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """以 ANCHOR=2026-05-10 12:00 UTC 为锚，验证 7 天窗内每天的计数。

    populated 数据落在 7 天窗内的（2026-05-04 ~ 2026-05-10）：
    - 6d 前（2026-05-04 12:00）：anthropic_news + simonw_blog 各 1
        ※ simonw 实际 6d 3h 前 = 2026-05-04 09:00 → 2026-05-04
    - 3d 前（2026-05-07 12:00）：dotey 1
    - 3d 8h 前（2026-05-07 04:00）：claude_api 1
    - 2d 前（2026-05-08 12:00）：anthropic 1
    - 2d 4h 前（2026-05-08 08:00）：finance_demo 1
    - 25h 前（2026-05-09 11:00）：simonw 1
    - 23h 前（2026-05-09 13:00）：anthropic 1
    - 12h 前（2026-05-10 00:00）：dotey 1
    - 1h 前（2026-05-10 11:00）：anthropic 1
    - 1h30m 前（2026-05-10 10:30）：claude_api 1

    注：8d 前那条（anthropic 8d）= 2026-05-02 12:00，落在 cutoff(2026-05-04) 之外，
    不计入。

    cutoff = today(2026-05-10) - 6 = 2026-05-04 起。
    """
    db_path, _conn = populated_raw_db
    home = db_path.parent
    _freeze_now(monkeypatch)

    result = _run(_make_app(), "--home", str(home), "--json")
    assert result.exit_code == 0
    payload = json.loads(result.output)
    last7 = payload["last_7_days"]
    # 总长 7
    assert len(last7) == 7
    # 顺序升序
    dates = [r["date"] for r in last7]
    assert dates == sorted(dates)
    # 边界日期
    assert last7[0]["date"] == "2026-05-04"
    assert last7[-1]["date"] == "2026-05-10"

    counts = {r["date"]: r["count"] for r in last7}
    # 关键日点位（每日各 source 计数加和）
    assert counts["2026-05-04"] == 2  # anthropic 6d + simonw 6d3h
    assert counts["2026-05-05"] == 0  # 无数据
    assert counts["2026-05-06"] == 0
    assert counts["2026-05-07"] == 2  # dotey 3d + claude 3d8h
    assert counts["2026-05-08"] == 2  # anthropic 2d + finance 2d4h
    assert counts["2026-05-09"] == 2  # simonw 25h + anthropic 23h
    assert counts["2026-05-10"] == 3  # dotey 12h + anthropic 1h + claude 1h30m

    # 加和 = 11（其余 9 行落在 7 天窗外）
    assert sum(r["count"] for r in last7) == 11


def test_stats_by_source_type_domain(
    populated_raw_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """domain_tags 多 domain 行用 json_each 展开后计入多组。

    populated_raw_db 数据：
    - 18 行 ['ai']（其中 rss × 14 + web × 4 ≠ 16+4？需重新看）
    - 1 行 ['ai','finance']（dotey 14d）
    - 1 行 ['finance']（finance_demo 2d）

    实际分桶（各 source_type 来自 fixture，rss × 16 + web × 4）：
    - rss + ai: 15（14 行 ['ai'] + 1 行 ['ai','finance'] 在 ai 计 1）
    - rss + finance: 2（dotey 多 domain + finance_demo 单 domain）
    - web + ai: 4（claude_api 4 行都是 ['ai']）
    """
    db_path, _conn = populated_raw_db
    home = db_path.parent
    _freeze_now(monkeypatch)

    result = _run(_make_app(), "--home", str(home), "--json")
    assert result.exit_code == 0
    payload = json.loads(result.output)
    by_td = payload["by_source_type_domain"]
    by_key = {(r["source_type"], r["domain"]): r["count"] for r in by_td}

    # rss × ai：15 行（rss 全 16 行里减去 finance_demo 那 1 行）
    assert by_key[("rss", "ai")] == 15
    # rss × finance：dotey 14d（双 domain）+ finance_demo 2d = 2
    assert by_key[("rss", "finance")] == 2
    # web × ai：claude_api 全 4 行
    assert by_key[("web", "ai")] == 4
    # web × finance 不应存在
    assert ("web", "finance") not in by_key


def test_stats_thousands_separator_in_human_view(
    tmp_raw_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """人类视图数字千位分隔（``1,234``）。

    populated_raw_db 数太小看不出，单独灌 1500 行同 source 跑这条断言。
    """
    db_path, conn = tmp_raw_db
    home = db_path.parent
    _freeze_now(monkeypatch)

    base_fetched = ANCHOR - timedelta(hours=2)
    rows = []
    for i in range(1500):
        title = f"big-title-{i}"
        body = f"big-body-{i}"
        content_hash = hashlib.sha256((title + body[:500]).encode("utf-8")).hexdigest()
        rows.append(
            (
                "rss",
                "big_source",
                "kol",
                f"big-{i}",
                f"https://example.com/big-{i}",
                f"hash-url-big-{i}",
                content_hash,
                title,
                body,
                None,
                base_fetched.isoformat(),
                json.dumps(["ai"]),
                None,
            )
        )
    conn.executemany(
        """
        INSERT INTO articles_raw (
            source_type, source_id, source_tier, external_id,
            url, url_canonical_hash, content_hash, title, body,
            published_at, fetched_at, domain_tags, is_long_form
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()

    result = _run(_make_app(), "--home", str(home))
    assert result.exit_code == 0
    out = result.output
    # 总条数 1,500 应带千位分隔
    assert "articles: 1,500" in out
    # 排行里 big_source 计数也带千位分隔
    assert "1,500" in out


def test_stats_reads_sources_yaml_counts(
    populated_raw_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sources.yaml 存在时，enabled / disabled 计数应来自 yaml 而非 raw.db。"""
    db_path, _conn = populated_raw_db
    home = db_path.parent
    _freeze_now(monkeypatch)

    # 灌一个最小 sources.yaml：rss 2 enabled + 1 disabled，web 1 enabled
    yaml_content = """
rss:
  - id: a
    url: http://example.com/a
    tier: kol
    domain: [ai]
  - id: b
    url: http://example.com/b
    tier: kol
    domain: [ai]
    enabled: false
  - id: c
    url: http://example.com/c
    tier: kol
    domain: [ai]
web:
  - id: d
    url: http://example.com/d
    selector: auto
    tier: official_first_party
    domain: [ai]
""".lstrip()
    (home / "sources.yaml").write_text(yaml_content, encoding="utf-8")

    result = _run(_make_app(), "--home", str(home), "--json")
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["total"]["enabled_sources"] == 3  # a, c, d
    assert payload["total"]["disabled_sources"] == 1  # b


def test_stats_sources_yaml_missing_falls_back_to_zero(
    populated_raw_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sources.yaml 不存在时 enabled/disabled 退化为 0，不影响其它 panel。"""
    db_path, _conn = populated_raw_db
    home = db_path.parent
    _freeze_now(monkeypatch)
    # 不创建 sources.yaml

    result = _run(_make_app(), "--home", str(home), "--json")
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["total"]["enabled_sources"] == 0
    assert payload["total"]["disabled_sources"] == 0
    # 其它 panel 仍正常
    assert payload["total"]["articles"] == 20
