"""sources/_io.py 测试：ruamel.yaml round-trip + 6 helper 行为。"""

from __future__ import annotations

from pathlib import Path

import pytest

from news_collector.commands.sources import _io
from news_collector.commands.sources._io import (
    SourceIdConflictError,
    SourceKindError,
    find_source,
    load_yaml,
    remove_source,
    rename_source,
    save_yaml,
    update_source,
    upsert_source,
)

# ------------- 测试 fixture：带注释的 sources.yaml --------------

SAMPLE_YAML = """\
# 顶部注释：信源清单
# 设计: see DECISIONS.md
rss:
  # X / KOL 段
  - id: x_dotey  # 宝玉，AI 领域 KOL
    url: "http://localhost:1200/twitter/user/dotey?format=atom"
    tier: kol
    domain: [ai]
  - id: x_kepano  # kepano，Obsidian CEO
    url: "http://localhost:1200/twitter/user/kepano?format=atom"
    tier: kol
    domain: [ai]

  # GitHub atom 段
  - id: gh_anthropic
    url: "https://github.com/anthropics/anthropic-sdk-python/releases.atom"
    tier: official_first_party
    domain: [ai]

# web 段
web:
  - id: anthropic_news  # 已迁 RSSHub，临时停用
    url: "https://www.anthropic.com/news"
    selector: auto
    tier: official_first_party
    domain: [ai]
    enabled: false
"""


@pytest.fixture
def sources_path(tmp_path: Path) -> Path:
    p = tmp_path / "sources.yaml"
    p.write_text(SAMPLE_YAML, encoding="utf-8")
    return p


# ------------- load_yaml --------------


def test_load_yaml_returns_commented_map(sources_path: Path) -> None:
    data = load_yaml(sources_path)
    assert "rss" in data
    assert "web" in data
    assert len(data["rss"]) == 3
    assert len(data["web"]) == 1


def test_load_yaml_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_yaml(tmp_path / "absent.yaml")


def test_load_yaml_empty_file_returns_empty_map(tmp_path: Path) -> None:
    p = tmp_path / "empty.yaml"
    p.write_text("", encoding="utf-8")
    data = load_yaml(p)
    assert dict(data) == {}


def test_load_yaml_top_level_non_mapping_raises(tmp_path: Path) -> None:
    p = tmp_path / "list.yaml"
    p.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_yaml(p)


# ------------- save_yaml + 注释保留 --------------


def test_save_yaml_roundtrip_preserves_comments(sources_path: Path) -> None:
    data = load_yaml(sources_path)
    save_yaml(sources_path, data)
    text = sources_path.read_text(encoding="utf-8")
    # 关键注释存在
    assert "# 顶部注释：信源清单" in text
    assert "# X / KOL 段" in text
    assert "# 宝玉，AI 领域 KOL" in text
    assert "# kepano，Obsidian CEO" in text
    assert "# GitHub atom 段" in text
    assert "# web 段" in text
    assert "# 已迁 RSSHub，临时停用" in text


def test_save_yaml_creates_parent_dir(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "dir" / "sources.yaml"
    data = load_yaml_from_str(SAMPLE_YAML)
    save_yaml(target, data)
    assert target.exists()


def load_yaml_from_str(text: str):
    """test helper：从字符串构造 CommentedMap（不命名 setup_/teardown_，避开 KNOWLEDGE-LOG #13）。"""
    yaml_rt = _io._yaml()
    return yaml_rt.load(text)


# ------------- find_source --------------


def test_find_source_hits_rss(sources_path: Path) -> None:
    data = load_yaml(sources_path)
    found = find_source(data, "x_dotey")
    assert found is not None
    kind, idx, item = found
    assert kind == "rss"
    assert idx == 0
    assert item["url"].endswith("dotey?format=atom")


def test_find_source_hits_web(sources_path: Path) -> None:
    data = load_yaml(sources_path)
    found = find_source(data, "anthropic_news")
    assert found is not None
    kind, _, item = found
    assert kind == "web"
    assert item["selector"] == "auto"


def test_find_source_missing_returns_none(sources_path: Path) -> None:
    data = load_yaml(sources_path)
    assert find_source(data, "nonexistent") is None


# ------------- upsert_source --------------


def test_upsert_appends_to_existing_kind(sources_path: Path) -> None:
    data = load_yaml(sources_path)
    new_item = {"id": "x_new", "url": "http://example.com/atom", "tier": "kol", "domain": ["ai"]}
    upsert_source(data, "rss", new_item)
    assert len(data["rss"]) == 4
    assert data["rss"][-1]["id"] == "x_new"


def test_upsert_creates_kind_if_absent(tmp_path: Path) -> None:
    p = tmp_path / "min.yaml"
    p.write_text("rss: []\n", encoding="utf-8")
    data = load_yaml(p)
    item = {"id": "test_web", "url": "https://example.com", "tier": "kol", "domain": ["ai"]}
    upsert_source(data, "web", item)
    assert "web" in data
    assert data["web"][0]["id"] == "test_web"


def test_upsert_id_conflict_raises(sources_path: Path) -> None:
    data = load_yaml(sources_path)
    with pytest.raises(SourceIdConflictError):
        upsert_source(data, "rss", {"id": "x_dotey", "url": "http://x"})


def test_upsert_id_conflict_cross_kind(sources_path: Path) -> None:
    """rss 已有 x_dotey，往 web 加同 id 也应该冲突（全局唯一）。"""
    data = load_yaml(sources_path)
    with pytest.raises(SourceIdConflictError):
        upsert_source(data, "web", {"id": "x_dotey", "url": "http://x"})


def test_upsert_invalid_kind_raises(sources_path: Path) -> None:
    data = load_yaml(sources_path)
    with pytest.raises(SourceKindError):
        upsert_source(data, "social", {"id": "x", "url": "http://x"})


def test_upsert_missing_id_raises(sources_path: Path) -> None:
    data = load_yaml(sources_path)
    with pytest.raises(ValueError):
        upsert_source(data, "rss", {"url": "http://x"})


# ------------- remove_source --------------


def test_remove_existing_returns_true(sources_path: Path) -> None:
    data = load_yaml(sources_path)
    assert remove_source(data, "x_kepano") is True
    assert find_source(data, "x_kepano") is None
    assert len(data["rss"]) == 2


def test_remove_missing_returns_false(sources_path: Path) -> None:
    data = load_yaml(sources_path)
    assert remove_source(data, "nonexistent") is False


# ------------- update_source --------------


def test_update_mutator_changes_field(sources_path: Path) -> None:
    data = load_yaml(sources_path)

    def set_disabled(item):
        item["enabled"] = False

    assert update_source(data, "x_dotey", set_disabled) is True
    item = find_source(data, "x_dotey")
    assert item is not None
    assert item[2]["enabled"] is False


def test_update_missing_returns_false(sources_path: Path) -> None:
    data = load_yaml(sources_path)
    called = []
    assert update_source(data, "nonexistent", lambda i: called.append(1)) is False
    assert called == []


# ------------- rename_source --------------


def test_rename_existing_id(sources_path: Path) -> None:
    data = load_yaml(sources_path)
    assert rename_source(data, "x_dotey", "x_dotey_renamed") is True
    assert find_source(data, "x_dotey") is None
    assert find_source(data, "x_dotey_renamed") is not None


def test_rename_id_conflict_raises(sources_path: Path) -> None:
    data = load_yaml(sources_path)
    with pytest.raises(SourceIdConflictError):
        rename_source(data, "x_dotey", "x_kepano")


def test_rename_same_id_raises(sources_path: Path) -> None:
    data = load_yaml(sources_path)
    with pytest.raises(SourceIdConflictError):
        rename_source(data, "x_dotey", "x_dotey")


def test_rename_old_id_missing_returns_false(sources_path: Path) -> None:
    data = load_yaml(sources_path)
    assert rename_source(data, "nonexistent", "new_id") is False


# ------------- 综合：mutate + save 后注释仍在 --------------


def test_mutate_then_save_keeps_unrelated_comments(sources_path: Path) -> None:
    data = load_yaml(sources_path)
    update_source(data, "x_dotey", lambda i: i.__setitem__("tier", "official_first_party"))
    remove_source(data, "x_kepano")
    upsert_source(
        data,
        "rss",
        {"id": "x_new", "url": "http://example.com/feed", "tier": "kol", "domain": ["ai"]},
    )
    save_yaml(sources_path, data)

    text = sources_path.read_text(encoding="utf-8")
    # 顶层 + 段注释保留
    assert "# 顶部注释：信源清单" in text
    assert "# X / KOL 段" in text
    assert "# GitHub atom 段" in text
    assert "# web 段" in text
    # 未受影响的 source 注释保留
    assert "# 宝玉，AI 领域 KOL" in text
    assert "# 已迁 RSSHub，临时停用" in text
    # mutate 生效
    assert "tier: official_first_party" in text  # x_dotey 改了
    # remove 生效
    assert "x_kepano" not in text
    # upsert 生效
    assert "x_new" in text
