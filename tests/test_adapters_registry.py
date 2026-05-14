"""``adapters.registry`` 单一真相源契约测试（s10 Step 0.5 产出）。

锁定：
- ADAPTER_REGISTRY 与 supported_types 同源同序
- 临时往 ADAPTER_REGISTRY 注入第 3 类型时，sources / commands 模块自动识别
  （而非读"导入时绑定"的 SOURCE_KINDS 常量副本）

这套契约保证未来加新 source_type（twikit / xhs / weibo 等）时 sprint 工作量
是 O(1)：只在 registry.py 加一行映射，sources.yaml 顶层段、doctor 抽样、
fetch 分桶、list_show 列名、_io find/upsert 都自动识别。
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml as pyyaml

from newsbox.adapters import ADAPTER_REGISTRY, supported_types
from newsbox.adapters.rss_adapter import RSSAdapter
from newsbox.adapters.web_adapter import WebAdapter


# ---- 基础契约 ---------------------------------------------------------------


def test_registry_contains_rss_and_web() -> None:
    """rss / web 默认都已注册到 ADAPTER_REGISTRY。"""
    assert "rss" in ADAPTER_REGISTRY
    assert "web" in ADAPTER_REGISTRY
    assert ADAPTER_REGISTRY["rss"] is RSSAdapter
    assert ADAPTER_REGISTRY["web"] is WebAdapter


def test_supported_types_matches_registry_keys() -> None:
    """``supported_types()`` 与 ``ADAPTER_REGISTRY.keys()`` 同源同序。"""
    assert supported_types() == tuple(ADAPTER_REGISTRY.keys())


def test_supported_types_returns_tuple() -> None:
    """返回不可变 tuple 防止误改。"""
    result = supported_types()
    assert isinstance(result, tuple)
    assert all(isinstance(k, str) for k in result)


def test_supported_types_reflects_runtime_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """运行时往 ADAPTER_REGISTRY 加 fakeadapter，supported_types 立刻反映。"""

    class _DummyAdapter:
        source_type = "fakeadapter"

        async def fetch(self, source, since):
            return []

    monkeypatch.setitem(ADAPTER_REGISTRY, "fakeadapter", _DummyAdapter)
    assert "fakeadapter" in supported_types()
    assert ADAPTER_REGISTRY["fakeadapter"] is _DummyAdapter


# ---- 与 newsbox.sources 集成 ------------------------------------------------


def test_iter_sources_picks_up_mock_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``sources.iter_sources`` 用 supported_types() 实时派生，识别 mock 第 3 类型。

    这是 Step 0.5 解耦审查的核心断言：函数内部 iterate 必须用 ``supported_types()``
    而非导入时绑定的 ``SOURCE_KINDS`` 常量，否则 mock 新类型时漏算其 yaml 段。
    """
    from newsbox import sources as src_mod

    class _DummyAdapter:
        source_type = "fakeadapter"

        async def fetch(self, source, since):
            return []

    monkeypatch.setitem(ADAPTER_REGISTRY, "fakeadapter", _DummyAdapter)

    yaml_content = {
        "rss": [
            {"id": "r1", "url": "http://example.com/feed", "enabled": True}
        ],
        "fakeadapter": [{"id": "f1", "url": "handle1", "enabled": True}],
    }
    yaml_path = tmp_path / "sources.yaml"
    yaml_path.write_text(pyyaml.safe_dump(yaml_content), encoding="utf-8")

    items = src_mod.iter_sources(yaml_path)
    types = {item["source_type"] for item in items}
    assert "rss" in types
    assert "fakeadapter" in types  # 关键：第 3 类型被识别


def test_list_sources_picks_up_mock_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``sources.list_sources`` 同样从 registry 派生 key 集。"""
    from newsbox import sources as src_mod

    class _DummyAdapter:
        source_type = "fakeadapter"

        async def fetch(self, source, since):
            return []

    monkeypatch.setitem(ADAPTER_REGISTRY, "fakeadapter", _DummyAdapter)

    yaml_content = {
        "rss": [
            {"id": "r1", "url": "http://example.com/feed", "enabled": True}
        ],
        "fakeadapter": [
            {"id": "f1", "url": "handle1", "enabled": True},
            {"id": "f2", "url": "handle2", "enabled": False},
        ],
    }
    yaml_path = tmp_path / "sources.yaml"
    yaml_path.write_text(pyyaml.safe_dump(yaml_content), encoding="utf-8")

    stats = src_mod.list_sources(yaml_path)
    assert "rss" in stats
    assert "fakeadapter" in stats
    assert stats["fakeadapter"]["total"] == 2
    assert stats["fakeadapter"]["enabled"] == 1


# ---- 与 _io 集成 -----------------------------------------------------------


def test_io_find_source_picks_up_mock_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_io.find_source`` 用 supported_types() 派生，能在 fakeadapter 段定位。"""
    from newsbox.commands.sources import _io

    class _DummyAdapter:
        source_type = "fakeadapter"

        async def fetch(self, source, since):
            return []

    monkeypatch.setitem(ADAPTER_REGISTRY, "fakeadapter", _DummyAdapter)

    yaml_path = tmp_path / "sources.yaml"
    yaml_path.write_text(
        "rss:\n"
        "  - id: r1\n"
        "    url: http://example.com/feed\n"
        "    tier: kol\n"
        "    domain: [ai]\n"
        "fakeadapter:\n"
        "  - id: f1\n"
        "    url: handle1\n"
        "    tier: kol\n"
        "    domain: [ai]\n",
        encoding="utf-8",
    )

    data = _io.load_yaml(yaml_path)
    found = _io.find_source(data, "f1")
    assert found is not None
    kind, idx, item = found
    assert kind == "fakeadapter"
    assert item["id"] == "f1"


def test_io_upsert_unknown_kind_rejected(
    tmp_path: Path,
) -> None:
    """upsert 时 kind 不在已注册类型集合内抛 SourceKindError。"""
    from newsbox.commands.sources import _io

    yaml_path = tmp_path / "sources.yaml"
    yaml_path.write_text("rss: []\n", encoding="utf-8")
    data = _io.load_yaml(yaml_path)

    with pytest.raises(_io.SourceKindError):
        _io.upsert_source(data, "no_such_type", {"id": "x", "url": "u"})


# ---- 与 doctor 集成 ---------------------------------------------------------


def test_doctor_by_type_derives_from_registry() -> None:
    """doctor 模块内的 by_type 字典必须从 ADAPTER_REGISTRY 派生（不再有 _ADAPTER_REGISTRY 副本）。"""
    from newsbox.commands import doctor as doc_mod

    # 确认 doctor 不再持有独立副本
    assert not hasattr(doc_mod, "_ADAPTER_REGISTRY")
    # 确认从 adapters 拿到的就是真相源
    assert doc_mod.ADAPTER_REGISTRY is ADAPTER_REGISTRY


# ---- 与 pipeline/fetch 集成 -------------------------------------------------


def test_pipeline_fetch_uses_registry() -> None:
    """pipeline/fetch 模块的 ADAPTER_REGISTRY 与 adapters 单一真相源同体。"""
    from newsbox.pipeline import fetch as fetch_mod

    assert fetch_mod.ADAPTER_REGISTRY is ADAPTER_REGISTRY
