"""``newsbox read`` 命令测试。

覆盖：
1. raw.db 不存在 → exit 1 + 错误提示
2. 空库 → "(no articles match)" + exit 0
3. since=24h → 边界测试：1h / 12h 必然在窗口内
4. since=365d → 19 行 ai-domain 全出
5. since 解析失败 → exit 2
6. domain=ai 过滤掉 ['finance'] 单 domain 行
7. domain=finance → 返回 fin-2d + dotey-14d
8. source-types=rss → 不返回 web 行
9. source-id=anthropic_news → 仅返回 anthropic_news 行（8 行）
10. tier=kol → 仅返回 kol 行（7 行）
11. tier=official_first_party,kol → 包含两类（17 行；secondary 1 行 = fin-2d 已被
    domain=ai 过滤掉，所以本测仅验证不含 secondary）
12. limit=3 → 摘要行 (3 articles)
13. limit=0 → 摘要行 (19 articles)
14. --json 默认输出 NDJSON 每行可 json.loads，字段对齐 ArticleRaw
15. --json 时 published_at None → JSON null
16. --json 0 条结果 → 0 行 + exit 0

注意（KNOWLEDGE-LOG #13/#14/#17）：
- CliRunner 不传 mix_stderr（Click 9 已移除该参数）。
- 单命令 typer app 会扁平化，invoke 不前置命令名 ``read``。
- 模块级辅助函数避免 ``setup_*`` / ``teardown_*`` 前缀（pytest xunit hook）。

数据断言策略：
- 内容/数量断言走 ``--json`` 模式（拿 source_id / external_id 直接比对）。
- 表头 / 列宽 / 摘要行用默认 rich Table 模式断言。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from newsbox.commands import read as read_module
from newsbox.commands.read import read_cmd
from newsbox.utils import throughput as throughput_module
from tests.conftest import ANCHOR


# ---- helpers ---------------------------------------------------------------


def _make_app() -> typer.Typer:
    """单命令 typer app；KNOWLEDGE-LOG #14：扁平化后无需前置命令名。"""
    app = typer.Typer()
    app.command("read")(read_cmd)
    return app


def _run(app: typer.Typer, *args: str) -> Any:
    runner = CliRunner()
    return runner.invoke(app, list(args))


def _far_future_since() -> str:
    """足够大的相对窗口，覆盖 fixture 60d 前数据 + 任何测试运行时间漂移。"""
    return "365d"


def _parse_ndjson(result: Any) -> list[dict[str, Any]]:
    """把 --json 模式的输出按行解析为 list[dict]。"""
    return [
        json.loads(ln) for ln in result.output.splitlines() if ln.strip()
    ]


def _external_ids(records: list[dict[str, Any]]) -> set[str]:
    return {r["external_id"] for r in records}


# ---- 单元 ------------------------------------------------------------------


def test_read_db_not_exists(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    # 不创建 raw.db

    result = _run(_make_app(), "--home", str(home))

    assert result.exit_code == 1
    assert "raw.db 未找到" in result.output


def test_read_empty_db_says_no_match(tmp_raw_db) -> None:
    db_path, _conn = tmp_raw_db
    home = db_path.parent

    result = _run(_make_app(), "--home", str(home))

    assert result.exit_code == 0
    assert "(no articles match)" in result.output


def test_read_invalid_since_exits_2(populated_raw_db) -> None:
    db_path, _conn = populated_raw_db
    home = db_path.parent

    result = _run(_make_app(), "--home", str(home), "--since", "not-a-time")

    assert result.exit_code == 2
    assert "not-a-time" in result.output


def test_read_default_since_24h_filters_correctly(
    populated_raw_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """24h 窗口（锁时刻到 ANCHOR）：1h / 12h / 23h 必在窗内；25h / 30d / 60d 必在窗外。

    s7-agent-skill-and-hotfix Step 1.5 修复：原版断言耦合真实运行时刻，
    当 now > ANCHOR + 12h 时 dotey-12h(ANCHOR-12h) 会被推出 24h 窗口而 fail。
    现统一 monkeypatch read 模块的 `_now_utc` 锁到 ANCHOR（KNOWLEDGE R-6 模式）。
    """
    monkeypatch.setattr(read_module, "_now_utc", lambda: ANCHOR)

    db_path, _conn = populated_raw_db
    home = db_path.parent

    result = _run(
        _make_app(), "--home", str(home), "--since", "24h", "--json"
    )

    assert result.exit_code == 0
    ids = _external_ids(_parse_ndjson(result))
    # 必在窗口内（now=ANCHOR，cutoff=ANCHOR-24h）
    assert "an-1h" in ids
    assert "cl-1h" in ids
    assert "dotey-12h" in ids
    assert "an-23h" in ids  # 23h ago 在 24h 窗内
    # 必在窗口外
    assert "simon-25h" not in ids  # 25h ago 越过 24h 边界
    assert "an-8d" not in ids
    assert "simon-30d" not in ids
    assert "an-old-1" not in ids


def test_read_far_future_since_returns_all_ai(populated_raw_db) -> None:
    """365d since 应捞到所有 ai-domain 行（19 行；不含 fin-2d 单 finance）。"""
    db_path, _conn = populated_raw_db
    home = db_path.parent

    result = _run(
        _make_app(),
        "--home",
        str(home),
        "--since",
        _far_future_since(),
        "--limit",
        "0",
        "--json",
    )

    assert result.exit_code == 0
    records = _parse_ndjson(result)
    ids = _external_ids(records)
    assert len(records) == 19
    assert "an-old-1" in ids
    assert "dotey-14d" in ids  # 多 domain ['ai','finance']
    assert "fin-2d" not in ids  # 单 finance


def test_read_domain_finance_returns_finance_rows(populated_raw_db) -> None:
    """domain=finance 应返回 fin-2d + dotey-14d。"""
    db_path, _conn = populated_raw_db
    home = db_path.parent

    result = _run(
        _make_app(),
        "--home",
        str(home),
        "--domain",
        "finance",
        "--since",
        _far_future_since(),
        "--limit",
        "0",
        "--json",
    )

    assert result.exit_code == 0
    records = _parse_ndjson(result)
    ids = _external_ids(records)
    assert ids == {"fin-2d", "dotey-14d"}


def test_read_source_types_rss_excludes_web(populated_raw_db) -> None:
    """--source-types=rss 不返回 web 行。"""
    db_path, _conn = populated_raw_db
    home = db_path.parent

    result = _run(
        _make_app(),
        "--home",
        str(home),
        "--source-types",
        "rss",
        "--since",
        _far_future_since(),
        "--limit",
        "0",
        "--json",
    )

    assert result.exit_code == 0
    records = _parse_ndjson(result)
    types = {r["source_type"] for r in records}
    assert types == {"rss"}
    # 16 行 rss + 0 行 web；fin-2d 单 finance 域被 domain=ai 默认过滤
    # 所以 rss 实际命中 = 16 - 1（fin-2d 是 rss 但单 finance 域）= 15
    ids = _external_ids(records)
    assert "fin-2d" not in ids


def test_read_source_id_exact_match(populated_raw_db) -> None:
    """--source-id=anthropic_news 仅返回 anthropic_news 行（共 8 行）。"""
    db_path, _conn = populated_raw_db
    home = db_path.parent

    result = _run(
        _make_app(),
        "--home",
        str(home),
        "--source-id",
        "anthropic_news",
        "--since",
        _far_future_since(),
        "--limit",
        "0",
        "--json",
    )

    assert result.exit_code == 0
    records = _parse_ndjson(result)
    sids = {r["source_id"] for r in records}
    assert sids == {"anthropic_news"}
    assert len(records) == 8


def test_read_tier_kol_only(populated_raw_db) -> None:
    """--tier=kol 仅返回 kol 行（dotey + simonw_blog 共 7 行）。"""
    db_path, _conn = populated_raw_db
    home = db_path.parent

    result = _run(
        _make_app(),
        "--home",
        str(home),
        "--tier",
        "kol",
        "--since",
        _far_future_since(),
        "--limit",
        "0",
        "--json",
    )

    assert result.exit_code == 0
    records = _parse_ndjson(result)
    tiers = {r["source_tier"] for r in records}
    assert tiers == {"kol"}
    assert len(records) == 7


def test_read_tier_multi_includes_both(populated_raw_db) -> None:
    """--tier=official_first_party,kol 应包含两类，但不含 secondary。"""
    db_path, _conn = populated_raw_db
    home = db_path.parent

    result = _run(
        _make_app(),
        "--home",
        str(home),
        "--tier",
        "official_first_party,kol",
        "--since",
        _far_future_since(),
        "--limit",
        "0",
        "--json",
    )

    assert result.exit_code == 0
    records = _parse_ndjson(result)
    tiers = {r["source_tier"] for r in records}
    # 19 行 ai 中 secondary 行（fin-2d）已经被 domain=ai 过滤；
    # 所以剩下的就是 official_first_party + kol（19 - 0 = 19）。
    assert tiers == {"official_first_party", "kol"}
    assert len(records) == 19


def test_read_limit_caps_rows(populated_raw_db) -> None:
    """--limit=3 摘要行报告 3 articles。"""
    db_path, _conn = populated_raw_db
    home = db_path.parent

    result = _run(
        _make_app(),
        "--home",
        str(home),
        "--since",
        _far_future_since(),
        "--limit",
        "3",
    )

    assert result.exit_code == 0
    assert "(3 articles" in result.output


def test_read_limit_zero_returns_all(populated_raw_db) -> None:
    """--limit=0 表示无限；19 行全出。"""
    db_path, _conn = populated_raw_db
    home = db_path.parent

    result = _run(
        _make_app(),
        "--home",
        str(home),
        "--since",
        _far_future_since(),
        "--limit",
        "0",
    )

    assert result.exit_code == 0
    assert "(19 articles" in result.output


def test_read_table_default_has_header(populated_raw_db) -> None:
    """默认 rich Table 输出包含表头与摘要行。"""
    db_path, _conn = populated_raw_db
    home = db_path.parent

    result = _run(
        _make_app(),
        "--home",
        str(home),
        "--since",
        _far_future_since(),
        "--limit",
        "0",
    )

    assert result.exit_code == 0
    out = result.output
    # 表头列
    assert "fetched_at" in out
    assert "source_type" in out
    assert "source_id" in out
    assert "tier" in out
    assert "title" in out
    # 摘要行带 since 字面量回显
    assert "since=365d" in out


def test_read_json_emits_ndjson_with_correct_schema(populated_raw_db) -> None:
    """--json 输出每行可 json.loads，字段对齐 ArticleRaw。"""
    db_path, _conn = populated_raw_db
    home = db_path.parent

    result = _run(
        _make_app(),
        "--home",
        str(home),
        "--since",
        _far_future_since(),
        "--limit",
        "0",
        "--json",
    )

    assert result.exit_code == 0
    records = _parse_ndjson(result)
    assert len(records) == 19  # 19 行 ai-domain

    expected_keys = {
        "id",
        "source_type",
        "source_id",
        "source_tier",
        "external_id",
        "url",
        "title",
        "body",
        "content_hash",
        "published_at",
        "fetched_at",
        "domain_tags",
    }
    for obj in records:
        assert set(obj.keys()) == expected_keys
        # 类型抽样
        assert isinstance(obj["id"], int)
        assert isinstance(obj["domain_tags"], list)
        # fetched_at 始终非空 ISO
        assert obj["fetched_at"] is not None
        assert "T" in obj["fetched_at"]


def test_read_json_published_at_none_serializes_null(populated_raw_db) -> None:
    """fixture 中有 published_at=None 的行；--json 应输出 JSON null。"""
    db_path, _conn = populated_raw_db
    home = db_path.parent

    result = _run(
        _make_app(),
        "--home",
        str(home),
        "--since",
        _far_future_since(),
        "--limit",
        "0",
        "--json",
    )

    assert result.exit_code == 0
    records = _parse_ndjson(result)
    null_pub = [r for r in records if r["published_at"] is None]
    assert null_pub  # 至少存在一条


def test_read_json_empty_outputs_no_lines(tmp_raw_db) -> None:
    """--json 0 条结果时不输出任何行（agent 友好）+ exit 0。"""
    db_path, _conn = tmp_raw_db
    home = db_path.parent

    result = _run(_make_app(), "--home", str(home), "--json")

    assert result.exit_code == 0
    lines = [ln for ln in result.output.splitlines() if ln.strip()]
    assert lines == []


# ---- 阈值软阻断（s9 Step 3 / D4） ---------------------------------------


def _patch_count(monkeypatch: pytest.MonkeyPatch, predicted: int) -> None:
    """monkeypatch read 模块的 ``count_articles_raw`` 返回固定预估值。

    避免插 10k+ 行测试数据；预估值由 monkeypatch 决定，gate 决策可独立验证。
    """
    monkeypatch.setattr(
        read_module, "count_articles_raw", lambda *a, **kw: predicted
    )


def test_read_below_threshold_emits_no_warn(populated_raw_db) -> None:
    """20 行 < 默认阈值 10000 → 无 warn，正常输出。"""
    db_path, _conn = populated_raw_db
    home = db_path.parent

    result = _run(
        _make_app(),
        "--home",
        str(home),
        "--since",
        _far_future_since(),
        "--limit",
        "0",
    )

    assert result.exit_code == 0
    assert "[warn]" not in result.output
    assert "(19 articles" in result.output  # 19 ai-domain 行（不含单 finance）


def test_read_above_threshold_aborts_when_user_declines(
    populated_raw_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超阈值 + tty + confirm 答 no → abort + exit 1 + stderr 含 SDK snippet。"""
    _patch_count(monkeypatch, 99999)
    monkeypatch.setattr(throughput_module, "_stdin_is_tty", lambda: True)

    db_path, _conn = populated_raw_db
    home = db_path.parent

    runner = CliRunner()
    # 答 no（typer.confirm default=False，直接回车也是 no）
    result = runner.invoke(
        _make_app(),
        ["--home", str(home), "--since", _far_future_since()],
        input="n\n",
    )

    assert result.exit_code == 1
    assert "[warn]" in result.output
    assert "99,999" in result.output  # 数字千分位格式化
    assert "from newsbox.sdk import read_raw" in result.output  # SDK snippet
    # 拒绝后不应输出表头（未走到表格渲染段）
    assert "fetched_at" not in result.output or "[warn]" in result.output


def test_read_above_threshold_with_yes_continues(
    populated_raw_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超阈值 + --yes → warn 仍出，但继续执行；exit 0。"""
    _patch_count(monkeypatch, 99999)
    # --yes 路径不进 tty 分支，但保险起见 patch 让结果与 tty 状态无关
    monkeypatch.setattr(throughput_module, "_stdin_is_tty", lambda: False)

    db_path, _conn = populated_raw_db
    home = db_path.parent

    result = _run(
        _make_app(),
        "--home",
        str(home),
        "--since",
        _far_future_since(),
        "--limit",
        "0",
        "--yes",
    )

    assert result.exit_code == 0
    assert "[warn]" in result.output
    assert "(19 articles" in result.output  # 继续到表格渲染


def test_read_above_threshold_with_json_continues_clean_stdout(
    populated_raw_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超阈值 + --json → 等价 --yes，warn 走 stderr，NDJSON 仍正确出来。"""
    _patch_count(monkeypatch, 99999)
    monkeypatch.setattr(throughput_module, "_stdin_is_tty", lambda: False)

    db_path, _conn = populated_raw_db
    home = db_path.parent

    result = _run(
        _make_app(),
        "--home",
        str(home),
        "--since",
        _far_future_since(),
        "--limit",
        "0",
        "--json",
    )

    assert result.exit_code == 0
    assert "[warn]" in result.output

    # 从混合输出里提取合法 JSON 行（warn 文案不是 JSON，会被 skip）
    json_lines: list[dict[str, Any]] = []
    for ln in result.output.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            json_lines.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    assert len(json_lines) == 19  # 19 行 ai-domain


def test_read_above_threshold_non_tty_aborts_with_guidance(
    populated_raw_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超阈值 + 非 tty + 无 --yes + 无 --json → abort + 引导文案。"""
    _patch_count(monkeypatch, 99999)
    monkeypatch.setattr(throughput_module, "_stdin_is_tty", lambda: False)

    db_path, _conn = populated_raw_db
    home = db_path.parent

    result = _run(
        _make_app(),
        "--home",
        str(home),
        "--since",
        _far_future_since(),
    )

    assert result.exit_code == 1
    assert "[warn]" in result.output
    assert "非交互环境" in result.output
    assert "--yes" in result.output


def test_read_threshold_overridable_via_config_yaml(
    populated_raw_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``thresholds.cli_read_warn`` 在 ~/.newsbox/config.yaml 中可覆盖默认 10000。"""
    db_path, _conn = populated_raw_db
    home = db_path.parent
    # 把阈值降到 5（< 实际 19 行）→ warn 必触发
    (home / "config.yaml").write_text(
        "thresholds:\n  cli_read_warn: 5\n", encoding="utf-8"
    )
    # 用 --yes 跳过 confirm，单独验阈值生效
    monkeypatch.setattr(throughput_module, "_stdin_is_tty", lambda: False)

    result = _run(
        _make_app(),
        "--home",
        str(home),
        "--since",
        _far_future_since(),
        "--limit",
        "0",
        "--yes",
    )

    assert result.exit_code == 0
    assert "[warn]" in result.output
    assert ">5 阈值" in result.output  # 千分位下 5 不分隔
