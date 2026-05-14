"""sources.py 测试：seed_sources / list_sources / iter_sources。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from newsbox import sources as ns
from newsbox.adapters import supported_types


# ---------- seed_sources ----------


def test_seed_sources_copies_default(tmp_path: Path) -> None:
    target = tmp_path / "sources.yaml"
    out = ns.seed_sources(target)

    assert out == target.resolve()
    assert target.exists()
    # 字节级一致：copy2 保留内容；同时 yaml load 后等价
    assert target.read_bytes() == ns.DEFAULT_SEED_PATH.read_bytes()
    loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    # 顶层 2 类 key 应都落地
    assert {"rss", "web"} <= set(loaded.keys())


def test_seed_sources_creates_parent(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "deeper" / "sources.yaml"
    assert not target.parent.exists()

    out = ns.seed_sources(target)

    assert out == target.resolve()
    assert target.exists()
    assert target.parent.is_dir()


def test_seed_sources_force_required(tmp_path: Path) -> None:
    target = tmp_path / "sources.yaml"
    target.write_text("placeholder: true\n", encoding="utf-8")

    # 已存在 + 不带 force → 报错
    with pytest.raises(FileExistsError):
        ns.seed_sources(target)

    # 内容仍是占位（未被覆盖）
    assert target.read_text(encoding="utf-8") == "placeholder: true\n"

    # 带 force → 成功覆盖
    out = ns.seed_sources(target, force=True)
    assert out == target.resolve()
    assert target.read_bytes() == ns.DEFAULT_SEED_PATH.read_bytes()


def test_seed_sources_missing_seed(tmp_path: Path) -> None:
    target = tmp_path / "sources.yaml"
    nonexistent_seed = tmp_path / "nope.seed.yaml"
    with pytest.raises(FileNotFoundError):
        ns.seed_sources(target, seed_path=nonexistent_seed)


# ---------- list_sources ----------


def test_list_sources_real_seed() -> None:
    counts = ns.list_sources(ns.DEFAULT_SEED_PATH)

    # key 集合 = 所有已注册 source_type（s10 Step 0.5 起派生于 ADAPTER_REGISTRY）
    assert set(counts.keys()) == set(supported_types())

    # 与 src/newsbox/data/sources.seed.yaml 实际计数一致（s10 Step 6 后）：
    #   rss 22 / 21 启用（infoq disabled）
    #     4 Reddit + 2 newsletter + 4 厂商博客 + 2 status + 3 社区聚合 +
    #     4 GitHub release atom + 3 RSSHub 厂商博客/changelog = 22
    #   web 4 / 3 启用（openai_api_changelog disabled）
    #     原 7 条扣除 3 条迁 RSSHub = 4（claude_api / claude_product /
    #     gemini_api_changelog / openai_api_changelog）
    #   twikit 26 / 26（s10 Step 6 迁入：20 X KOL + 6 X 官方账号）
    assert counts["rss"] == {"total": 22, "enabled": 21}
    assert counts["web"] == {"total": 4, "enabled": 3}
    assert counts["twikit"] == {"total": 26, "enabled": 26}


def test_list_sources_disabled_counted(tmp_path: Path) -> None:
    yaml_path = tmp_path / "sources.yaml"
    yaml_path.write_text(
        "rss:\n"
        "  - id: a\n"
        "    enabled: false\n"
        "  - id: b\n",
        encoding="utf-8",
    )

    counts = ns.list_sources(yaml_path)

    assert counts["rss"] == {"total": 2, "enabled": 1}
    # web 类 0/0
    assert counts["web"] == {"total": 0, "enabled": 0}


def test_list_sources_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        ns.list_sources(tmp_path / "nope.yaml")


def test_list_sources_unknown_top_keys_ignored(tmp_path: Path) -> None:
    """顶层未识别 key 不影响；已注册 source_type 仍全部出现。"""
    yaml_path = tmp_path / "sources.yaml"
    yaml_path.write_text(
        "rss:\n"
        "  - id: a\n"
        "future_kind:\n"
        "  - id: zzz\n"
        "meta:\n"
        "  version: 1\n",
        encoding="utf-8",
    )

    counts = ns.list_sources(yaml_path)

    # key 集合 = 所有已注册 source_type（派生于 ADAPTER_REGISTRY）
    assert set(counts.keys()) == set(supported_types())
    assert counts["rss"] == {"total": 1, "enabled": 1}
    # 其他已注册类型 yaml 中无对应段 → 计数全 0
    for kind in supported_types():
        if kind == "rss":
            continue
        assert counts[kind] == {"total": 0, "enabled": 0}


def test_list_sources_invalid_yaml(tmp_path: Path) -> None:
    yaml_path = tmp_path / "broken.yaml"
    # 尾部冒号 + 不闭合的 flow → 解析报错
    yaml_path.write_text("rss: [unclosed", encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        ns.list_sources(yaml_path)


# ---------- iter_sources ----------


def test_iter_sources_real_seed_count_matches_list() -> None:
    items = ns.iter_sources(ns.DEFAULT_SEED_PATH)
    counts = ns.list_sources(ns.DEFAULT_SEED_PATH)

    # 总数应等于各类 enabled 之和
    expected_total = sum(c["enabled"] for c in counts.values())
    assert len(items) == expected_total

    # 每条都有 source_type 字段且取值合法（派生于 ADAPTER_REGISTRY）
    valid_types = set(supported_types())
    for item in items:
        assert "source_type" in item
        assert item["source_type"] in valid_types
        assert "id" in item


def test_iter_sources_skips_disabled(tmp_path: Path) -> None:
    yaml_path = tmp_path / "sources.yaml"
    yaml_path.write_text(
        "rss:\n"
        "  - id: a\n"
        "    url: https://a.com/feed\n"
        "  - id: b\n"
        "    url: https://b.com/feed\n"
        "    enabled: false\n",
        encoding="utf-8",
    )
    items = ns.iter_sources(yaml_path)
    assert len(items) == 1
    assert items[0]["id"] == "a"
    assert items[0]["source_type"] == "rss"


def test_iter_sources_preserves_yaml_order_within_kind(tmp_path: Path) -> None:
    yaml_path = tmp_path / "sources.yaml"
    yaml_path.write_text(
        "web:\n"
        "  - id: web_first\n"
        "    url: https://a.com/news\n"
        "  - id: web_second\n"
        "    url: https://b.com/news\n"
        "rss:\n"
        "  - id: rss_one\n"
        "    url: https://x.com/feed\n",
        encoding="utf-8",
    )
    items = ns.iter_sources(yaml_path)
    # 2 类按 SOURCE_KINDS 固定顺序：rss → web
    # 故 rss_one 在 web_first / web_second 前
    assert [i["id"] for i in items] == ["rss_one", "web_first", "web_second"]


def test_iter_sources_keeps_kind_specific_fields(tmp_path: Path) -> None:
    yaml_path = tmp_path / "sources.yaml"
    yaml_path.write_text(
        "web:\n"
        "  - id: claude_changelog\n"
        "    mode: changelog_page\n"
        "    url: https://platform.claude.com/docs/release-notes/overview\n"
        "    markdown_url: https://platform.claude.com/docs/release-notes/overview.md\n"
        "    tier: official_first_party\n"
        "    domain: [ai]\n",
        encoding="utf-8",
    )
    items = ns.iter_sources(yaml_path)
    assert items == [
        {
            "source_type": "web",
            "id": "claude_changelog",
            "mode": "changelog_page",
            "url": "https://platform.claude.com/docs/release-notes/overview",
            "markdown_url": "https://platform.claude.com/docs/release-notes/overview.md",
            "tier": "official_first_party",
            "domain": ["ai"],
        }
    ]


def test_iter_sources_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        ns.iter_sources(tmp_path / "nope.yaml")


# ---------- sources seed --json （s9 Step 2） ----------

import json as _json  # noqa: E402

from typer.testing import CliRunner  # noqa: E402

from newsbox.cli import app as _cli_app  # noqa: E402


def test_seed_cmd_json_ok(tmp_path: Path) -> None:
    """``sources seed --json`` 成功路径输出 ok=True + counts。"""
    home = tmp_path / ".newsbox"
    home.mkdir()
    runner = CliRunner()
    r = runner.invoke(
        _cli_app, ["sources", "seed", "--home", str(home), "--json"]
    )
    assert r.exit_code == 0, r.output
    payload = _json.loads(r.stdout)
    assert payload["ok"] is True
    assert payload["message"] == "seed loaded"
    details = payload["details"]
    assert "path" in details
    assert details["total"] > 0
    assert "rss" in details["counts"]
    assert "web" in details["counts"]


def test_seed_cmd_json_already_exists(tmp_path: Path) -> None:
    """``sources seed --json`` 已存在且无 --force → ok=False + exit 1。"""
    home = tmp_path / ".newsbox"
    home.mkdir()
    (home / "sources.yaml").write_text("placeholder: true\n", encoding="utf-8")
    runner = CliRunner()
    r = runner.invoke(
        _cli_app, ["sources", "seed", "--home", str(home), "--json"]
    )
    assert r.exit_code == 1
    payload = _json.loads(r.stdout)
    assert payload["ok"] is False
    assert "seed failed" in payload["message"]
