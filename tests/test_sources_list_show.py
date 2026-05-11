"""``newsbox sources list`` / ``show`` 命令测试。

s4-sources-management Step 5 subagent A 产出。

注意事项（来自 KNOWLEDGE-LOG）：
- #13 测试模块禁用 ``setup_module`` / ``teardown_module`` 等 xunit hook 别名
- #14 typer 多命令 app 不会扁平化；本测试用整 CLI app（``sources list ...``）
- #17 Click 9 移除 ``CliRunner(mix_stderr=False)``；直接 ``CliRunner()``
"""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from newsbox.cli import app

# ------------- fixture：含 rss + web 的 sample sources.yaml ----------------

SAMPLE_YAML = """\
# 顶部注释：测试用 sources.yaml
rss:
  - id: x_dotey
    url: "http://localhost:1200/twitter/user/dotey?format=atom"
    tier: kol
    domain: [ai]
  - id: x_kepano
    url: "http://localhost:1200/twitter/user/kepano?format=atom"
    tier: kol
    domain: [ai]
  - id: gh_anthropic
    url: "https://github.com/anthropics/anthropic-sdk-python/releases.atom"
    tier: official_first_party
    domain: [ai]
  - id: rss_disabled
    url: "https://example.com/feed"
    tier: kol
    domain: [ai]
    enabled: false

web:
  - id: anthropic_news
    url: "https://www.anthropic.com/news"
    selector: auto
    tier: official_first_party
    domain: [ai]
    enabled: false
  - id: claude_api_release_notes
    url: "https://platform.claude.com/docs/release-notes/overview"
    mode: changelog_page
    markdown_url: "https://platform.claude.com/docs/release-notes/overview.md"
    tier: official_first_party
    domain: [ai]
"""


@pytest.fixture
def make_home(tmp_path: Path):
    """工厂：写一份 yaml 到 ``<tmp>/.newsbox/sources.yaml``，返回 home Path。"""

    def _factory(yaml_text: str = SAMPLE_YAML) -> Path:
        home = tmp_path / ".newsbox"
        home.mkdir(parents=True, exist_ok=True)
        (home / "sources.yaml").write_text(yaml_text, encoding="utf-8")
        return home

    return _factory


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ============================== sources list ==============================


def test_list_golden_path(runner: CliRunner, make_home) -> None:
    home = make_home()
    result = runner.invoke(app, ["sources", "list", "--home", str(home)])
    assert result.exit_code == 0, result.output

    out = result.output
    # 表头
    assert "TYPE" in out and "TIER" in out and "ID" in out and "URL" in out
    # 各 id 都该出现
    for sid in (
        "x_dotey",
        "x_kepano",
        "gh_anthropic",
        "rss_disabled",
        "anthropic_news",
        "claude_api_release_notes",
    ):
        assert sid in out
    # enabled 标记
    assert "✓" in out and "✗" in out
    # 末尾汇总行
    assert "total=6 enabled=4" in out
    assert "rss=3/4" in out and "web=1/2" in out


def test_list_filter_by_type(runner: CliRunner, make_home) -> None:
    home = make_home()
    result = runner.invoke(app, ["sources", "list", "--home", str(home), "--type", "web"])
    assert result.exit_code == 0, result.output

    # web 段两条都在
    assert "anthropic_news" in result.output
    assert "claude_api_release_notes" in result.output
    # rss 段不该出现
    assert "x_dotey" not in result.output
    assert "gh_anthropic" not in result.output
    # 汇总：rss=0/0 web=1/2
    assert "rss=0/0" in result.output
    assert "web=1/2" in result.output


def test_list_filter_by_tier(runner: CliRunner, make_home) -> None:
    home = make_home()
    result = runner.invoke(app, ["sources", "list", "--home", str(home), "--tier", "kol"])
    assert result.exit_code == 0, result.output

    # tier=kol 的 3 条（x_dotey / x_kepano / rss_disabled）应都在
    assert "x_dotey" in result.output
    assert "x_kepano" in result.output
    assert "rss_disabled" in result.output
    # tier!=kol 的不该在
    assert "gh_anthropic" not in result.output
    assert "anthropic_news" not in result.output
    # 总数 3
    assert "total=3" in result.output


def test_list_enabled_only(runner: CliRunner, make_home) -> None:
    home = make_home()
    result = runner.invoke(
        app, ["sources", "list", "--home", str(home), "--enabled-only"]
    )
    assert result.exit_code == 0, result.output

    # 4 条 enabled=true 出现
    assert "x_dotey" in result.output
    assert "claude_api_release_notes" in result.output
    # 2 条 disabled 不出现
    assert "rss_disabled" not in result.output
    assert "anthropic_news" not in result.output
    assert "✗" not in result.output
    assert "total=4 enabled=4" in result.output


def test_list_disabled_only(runner: CliRunner, make_home) -> None:
    home = make_home()
    result = runner.invoke(
        app, ["sources", "list", "--home", str(home), "--disabled-only"]
    )
    assert result.exit_code == 0, result.output

    assert "rss_disabled" in result.output
    assert "anthropic_news" in result.output
    # 启用项不出现
    assert "x_dotey" not in result.output
    assert "✓" not in result.output
    assert "total=2 enabled=0" in result.output


def test_list_enabled_disabled_mutex(runner: CliRunner, make_home) -> None:
    home = make_home()
    result = runner.invoke(
        app,
        [
            "sources",
            "list",
            "--home",
            str(home),
            "--enabled-only",
            "--disabled-only",
        ],
    )
    # 互斥：typer.BadParameter → exit code 2
    assert result.exit_code != 0
    # 报错信息走 stderr
    assert "互斥" in (result.stderr or "") or "互斥" in result.output


def test_list_invalid_type(runner: CliRunner, make_home) -> None:
    home = make_home()
    result = runner.invoke(
        app, ["sources", "list", "--home", str(home), "--type", "rss_kol"]
    )
    assert result.exit_code != 0


def test_list_missing_yaml(runner: CliRunner, tmp_path: Path) -> None:
    empty_home = tmp_path / "empty"
    empty_home.mkdir()
    result = runner.invoke(app, ["sources", "list", "--home", str(empty_home)])
    assert result.exit_code == 1
    err = result.stderr or result.output
    assert "sources.yaml 不存在" in err
    assert "sources seed" in err


def test_list_empty_yaml(runner: CliRunner, make_home) -> None:
    home = make_home("")  # 空文件
    result = runner.invoke(app, ["sources", "list", "--home", str(home)])
    assert result.exit_code == 0, result.output
    # 表头仍打印；无数据行；汇总 0/0
    assert "TYPE" in result.output
    assert "total=0 enabled=0" in result.output


# ============================== sources show ==============================


def test_show_golden_path(runner: CliRunner, make_home) -> None:
    home = make_home()
    result = runner.invoke(app, ["sources", "show", "x_dotey", "--home", str(home)])
    assert result.exit_code == 0, result.output

    out = result.output
    # 元信息行
    assert "[type]  rss" in out
    assert "[index] 0" in out  # x_dotey 是 rss 第 1 条
    assert "---" in out
    # 字段行
    assert "id: x_dotey" in out
    assert "tier: kol" in out
    # url 完整保留（show 不截断）
    assert "http://localhost:1200/twitter/user/dotey?format=atom" in out
    # domain inline 风格
    assert "domain: [ai]" in out
    # enabled 缺省视为 true，单独补一行
    assert "enabled: true (默认)" in out


def test_show_explicit_disabled_field(runner: CliRunner, make_home) -> None:
    home = make_home()
    result = runner.invoke(
        app, ["sources", "show", "anthropic_news", "--home", str(home)]
    )
    assert result.exit_code == 0, result.output

    out = result.output
    assert "[type]  web" in out
    assert "id: anthropic_news" in out
    assert "enabled: false" in out
    # 不应再补默认行（字段已显式存在）
    assert "(默认)" not in out


def test_show_web_with_extra_fields(runner: CliRunner, make_home) -> None:
    home = make_home()
    result = runner.invoke(
        app, ["sources", "show", "claude_api_release_notes", "--home", str(home)]
    )
    assert result.exit_code == 0, result.output

    out = result.output
    assert "[type]  web" in out
    assert "mode: changelog_page" in out
    assert "markdown_url:" in out
    # enabled 缺省补默认
    assert "enabled: true (默认)" in out


def test_show_not_found(runner: CliRunner, make_home) -> None:
    home = make_home()
    result = runner.invoke(
        app, ["sources", "show", "no_such_id", "--home", str(home)]
    )
    assert result.exit_code == 1
    err = result.stderr or result.output
    assert "source not found: no_such_id" in err


def test_show_missing_yaml(runner: CliRunner, tmp_path: Path) -> None:
    empty_home = tmp_path / "empty"
    empty_home.mkdir()
    result = runner.invoke(
        app, ["sources", "show", "x_dotey", "--home", str(empty_home)]
    )
    assert result.exit_code == 1
    err = result.stderr or result.output
    assert "sources.yaml 不存在" in err


# ----------------------- 回归：show 不应带出邻居段注释 -----------------------


_REGRESSION_YAML = """\
# 顶部注释
rss:
  - id: rss_a
    url: https://a.example/feed
    tier: kol
    domain: [ai]

# ============================================================
# web 段：这里写一大段无关注释，类似真实 sources.seed.yaml 的段间分隔
# 包含多行说明 / 历史决策记录
# 删除任何信源都不应带出这些注释
# ============================================================
web:
  - id: web_a
    url: https://web-a.example
    tier: official_first_party
    domain: [ai]
"""


def test_show_does_not_leak_neighbor_section_comments(
    runner: CliRunner, make_home
) -> None:
    """s4 Step 7 实测发现的 bug 回归：show 紧贴段落注释边界的 source 时，
    ``_dump_value`` 之前用 ruamel round-trip 直接 dump CommentedMap 会带出
    跨段邻居注释（比如 web 段头的 ``# ===`` 装饰行）。修法是先 ``_to_plain``
    剥离 ruamel 的注释附属，再 dump 纯结构。"""
    home = make_home(_REGRESSION_YAML)
    result = runner.invoke(app, ["sources", "show", "rss_a", "--home", str(home)])
    assert result.exit_code == 0, result.output
    out = result.output
    # 字段行正确显示
    assert "id: rss_a" in out
    assert "domain: [ai]" in out
    # 邻居段注释绝不应出现
    assert "===" not in out
    assert "web 段" not in out
    assert "历史决策记录" not in out
    # 顶部注释也不该出现
    assert "顶部注释" not in out


# ============================ --json （s9 Step 2） =========================

import json as _json  # noqa: E402


def test_list_json_golden(runner: CliRunner, make_home) -> None:
    """``sources list --json`` 输出按类型聚合的 schema。"""
    home = make_home()
    r = runner.invoke(app, ["sources", "list", "--home", str(home), "--json"])
    assert r.exit_code == 0, r.output
    payload = _json.loads(r.stdout)
    assert payload["total"] == 6
    assert payload["enabled_total"] == 4
    # 两个类型 key 都存在
    assert "rss" in payload and "web" in payload
    assert payload["rss"]["total"] == 4
    assert payload["rss"]["enabled"] == 3
    assert payload["web"]["total"] == 2
    assert payload["web"]["enabled"] == 1
    # sources 列表里每条带 id/tier/enabled/url
    rss_ids = {s["id"] for s in payload["rss"]["sources"]}
    assert {"x_dotey", "x_kepano", "gh_anthropic", "rss_disabled"} <= rss_ids
    # rss_disabled 标 enabled=False
    rss_disabled = next(
        s for s in payload["rss"]["sources"] if s["id"] == "rss_disabled"
    )
    assert rss_disabled["enabled"] is False


def test_list_json_missing_yaml(runner: CliRunner, tmp_path: Path) -> None:
    """``sources list --json`` yaml 不存在 → ok=False + exit 1。"""
    empty_home = tmp_path / "empty"
    empty_home.mkdir()
    r = runner.invoke(
        app, ["sources", "list", "--home", str(empty_home), "--json"]
    )
    assert r.exit_code == 1
    payload = _json.loads(r.stdout)
    assert payload["ok"] is False
    assert "sources.yaml not found" in payload["message"]


def test_list_json_mutually_exclusive_flags(
    runner: CliRunner, make_home
) -> None:
    """``sources list --enabled-only --disabled-only --json`` → emit_err + exit 2（codex P1 修）。"""
    home = make_home()
    r = runner.invoke(
        app,
        [
            "sources",
            "list",
            "--home",
            str(home),
            "--enabled-only",
            "--disabled-only",
            "--json",
        ],
    )
    assert r.exit_code == 2
    payload = _json.loads(r.stdout)
    assert payload["ok"] is False
    assert "互斥" in payload["message"]
    assert payload["details"]["flag_a"] == "--enabled-only"
    assert payload["details"]["flag_b"] == "--disabled-only"


def test_show_json_golden(runner: CliRunner, make_home) -> None:
    """``sources show --json`` 输出单条信源完整字段。"""
    home = make_home()
    r = runner.invoke(
        app, ["sources", "show", "x_dotey", "--home", str(home), "--json"]
    )
    assert r.exit_code == 0, r.output
    payload = _json.loads(r.stdout)
    assert payload["id"] == "x_dotey"
    assert payload["type"] == "rss"
    assert payload["index"] == 0
    assert payload["enabled"] is True  # 缺省视为 true
    assert payload["tier"] == "kol"
    assert payload["domain"] == ["ai"]
    assert payload["url"].startswith("http://localhost:1200")


def test_show_json_not_found(runner: CliRunner, make_home) -> None:
    """``sources show --json`` 未找到 → ok=False + exit 1。"""
    home = make_home()
    r = runner.invoke(
        app,
        ["sources", "show", "no_such_id", "--home", str(home), "--json"],
    )
    assert r.exit_code == 1
    payload = _json.loads(r.stdout)
    assert payload["ok"] is False
    assert "source not found" in payload["message"]
    assert payload["details"]["id"] == "no_such_id"
